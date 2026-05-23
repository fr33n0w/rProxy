#!/usr/bin/env python3
"""
rProxy Server — Reticulum HTTP Proxy Node
Announces itself on the Reticulum network and forwards HTTP requests.
"""

import os
import sys
import json
import time
import base64
import logging
import threading
import configparser
import argparse

import RNS
import requests as http_req

# ─── Constants ────────────────────────────────────────────────────────────────
APP_NAME    = "rproxy"
ASPECT      = "service"
CONFIG_FILE = "rproxy_server.ini"

DEFAULT_CONFIG = """
[reticulum]
# Leave empty to use default (~/.reticulum)
configdir =

[server]
# Path to persist the node identity
identity_path = ./rproxy_server.id

# Seconds between announces
announce_interval = 300

# HTTP request timeout (seconds)
request_timeout = 30

# Max response body size to relay (bytes) — default 4 MB
max_response_size = 4194304

# Comma-separated allowed HTTP methods
allowed_methods = GET,HEAD,POST,PUT,DELETE

# Optional: upstream HTTP proxy (e.g. http://proxy:3128)
# http_proxy =
"""

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [SERVER]  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rproxy.server")


# ─── Identity helpers ─────────────────────────────────────────────────────────
def load_or_create_identity(path: str) -> RNS.Identity:
    if os.path.exists(path):
        identity = RNS.Identity.from_file(path)
        log.info(f"Identity loaded  →  {path}")
    else:
        identity = RNS.Identity()
        identity.to_file(path)
        log.info(f"New identity created  →  {path}")
    return identity


# ─── Server class ─────────────────────────────────────────────────────────────
class RProxyServer:
    def __init__(self, config_path: str = CONFIG_FILE):
        self.cfg = configparser.ConfigParser()
        # Load defaults then override from file
        self.cfg.read_string(DEFAULT_CONFIG)
        if os.path.exists(config_path):
            self.cfg.read(config_path)
        else:
            log.warning(f"Config file not found ({config_path}), using defaults")

        # ── Reticulum ──
        rns_dir = self.cfg.get("reticulum", "configdir", fallback="").strip() or None
        self.reticulum = RNS.Reticulum(configdir=rns_dir)

        # ── Identity ──
        id_path = self.cfg.get("server", "identity_path", fallback="./rproxy_server.id")
        self.identity = load_or_create_identity(id_path)

        # ── Destination ──
        self.destination = RNS.Destination(
            self.identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            APP_NAME,
            ASPECT,
        )

        # Display name: rProxy-<last 6 hex chars of hash>
        self.hash_suffix   = self.destination.hash.hex()[-6:]
        self.display_name  = f"rProxy-{self.hash_suffix}"
        self.destination.set_default_app_data(self.display_name.encode("utf-8"))

        # ── Config ──
        self.announce_interval = self.cfg.getint("server", "announce_interval",  fallback=300)
        self.request_timeout   = self.cfg.getint("server", "request_timeout",    fallback=30)
        self.max_resp_size     = self.cfg.getint("server", "max_response_size",  fallback=4 * 1024 * 1024)

        methods_raw = self.cfg.get("server", "allowed_methods", fallback="GET,HEAD,POST")
        self.allowed_methods = {m.strip().upper() for m in methods_raw.split(",")}

        upstream = self.cfg.get("server", "http_proxy", fallback="").strip()
        self.proxies = {"http": upstream, "https": upstream} if upstream else None

        self.verify_ssl = self.cfg.getboolean("server", "verify_ssl", fallback=True)
        if not self.verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            log.warning("SSL verification DISABLED — verify_ssl=false in config")

        # ── Request handler (registered on Destination, not per-Link) ──
        self.destination.register_request_handler(
            "/fetch",
            self._handle_fetch,
            RNS.Destination.ALLOW_ALL,
        )

        # ── Callbacks ──
        self.destination.set_link_established_callback(self._on_link_established)

        log.info("─" * 50)
        log.info(f"  Display name  :  {self.display_name}")
        log.info(f"  Full hash     :  {self.destination.hash.hex()}")
        log.info(f"  Aspect        :  {APP_NAME}.{ASPECT}")
        log.info(f"  Announce      :  every {self.announce_interval}s")
        log.info(f"  Allowed       :  {', '.join(sorted(self.allowed_methods))}")
        log.info("─" * 50)

    # ── Public ────────────────────────────────────────────────────────────────
    def start(self):
        """Block forever, announcing periodically."""
        log.info("Server started. Announcing now…")
        while True:
            self.destination.announce()
            log.info(f"Announced as  {self.display_name}")
            time.sleep(self.announce_interval)

    # ── RNS callbacks ─────────────────────────────────────────────────────────
    def _on_link_established(self, link: RNS.Link):
        peer = RNS.prettyhexrep(link.hash)
        log.info(f"Link established  ←  {peer}")
        link.set_link_closed_callback(self._on_link_closed)

    def _on_link_closed(self, link: RNS.Link):
        peer = RNS.prettyhexrep(link.hash)
        log.info(f"Link closed  ←  {peer}")

    # ── Request handler ───────────────────────────────────────────────────────
    def _handle_fetch(self, path, data, request_id, link_id, remote_identity, requested_at):
        # ── Parse request ──
        try:
            req = json.loads(data.decode("utf-8"))
        except Exception as e:
            return self._err(f"Invalid JSON payload: {e}")

        method  = req.get("method",  "GET").upper()
        url     = req.get("url",     "").strip()
        headers = req.get("headers", {})
        body    = req.get("body",    None)

        # ── Validate ──
        if not url:
            return self._err("No URL provided")
        if method not in self.allowed_methods:
            return self._err(f"Method '{method}' not allowed on this proxy")
        if not url.startswith(("http://", "https://")):
            return self._err("Only http/https URLs are supported")

        # Strip hop-by-hop headers that shouldn't be forwarded
        HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate",
                      "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}
        clean_headers = {k: v for k, v in headers.items()
                         if k.lower() not in HOP_BY_HOP}

        log.info(f"Fetching  [{method}]  {url}")

        # ── HTTP fetch ──
        try:
            body_bytes = body.encode("utf-8") if isinstance(body, str) else body

            resp = http_req.request(
                method, url,
                headers=clean_headers,
                data=body_bytes,
                timeout=self.request_timeout,
                allow_redirects=True,
                proxies=self.proxies,
                verify=self.verify_ssl,
                stream=True,
            )

            # decode_content=True → urllib3 decompresses gzip/deflate/br automatically
            raw = resp.raw.read(self.max_resp_size, decode_content=True)
            truncated = resp.raw.read(1, decode_content=True) != b""

            # Detect charset from Content-Type, always return text with replace
            ct_hdr  = resp.headers.get("content-type", "")
            charset = "utf-8"
            if "charset=" in ct_hdr.lower():
                try:
                    part = ct_hdr.lower().split("charset=")[-1].split(";")[0].strip()
                    charset = part.strip("\"'")
                except Exception:
                    charset = "utf-8"
            try:
                body_out = raw.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                body_out = raw.decode("utf-8", errors="replace")
            body_enc = "text"

            result = {
                "ok":        True,
                "status":    resp.status_code,
                "reason":    resp.reason,
                "url":       resp.url,
                "headers":   dict(resp.headers),
                "body":      body_out,
                "encoding":  body_enc,
                "truncated": truncated,
            }
            log.info(f"Response  {resp.status_code}  ← {url}")
            return json.dumps(result).encode("utf-8")

        except http_req.exceptions.Timeout:
            return self._err(f"HTTP request timed out after {self.request_timeout}s")
        except http_req.exceptions.ConnectionError as e:
            return self._err(f"Connection error: {e}")
        except Exception as e:
            return self._err(str(e))

    # ── Helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _err(msg: str) -> bytes:
        log.warning(f"Error returned to client: {msg}")
        return json.dumps({"ok": False, "error": msg}).encode("utf-8")


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="rProxy — Reticulum HTTP Proxy Node")
    parser.add_argument("--config", default=CONFIG_FILE, metavar="FILE",
                        help=f"Config file (default: {CONFIG_FILE})")
    parser.add_argument("--generate-config", action="store_true",
                        help="Write a default config file and exit")
    args = parser.parse_args()

    if args.generate_config:
        with open(args.config, "w") as f:
            f.write(DEFAULT_CONFIG.strip() + "\n")
        print(f"Config written to {args.config}")
        sys.exit(0)

    try:
        server = RProxyServer(args.config)
        server.start()
    except KeyboardInterrupt:
        log.info("Shutting down.")
