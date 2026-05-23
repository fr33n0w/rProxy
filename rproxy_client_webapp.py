#!/usr/bin/env python3
"""
rProxy Client WebApp
Flask-based UI: discovers rProxy nodes via Reticulum announces,
lets the user pick an exit node and send HTTP requests through it.
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
from datetime import datetime, timezone

import RNS
from flask import Flask, render_template, jsonify, request as freq, abort

# ─── Constants ────────────────────────────────────────────────────────────────
APP_NAME    = "rproxy"
ASPECT      = "service"
CONFIG_FILE = "rproxy_client.ini"

DEFAULT_CONFIG = """
[reticulum]
# Leave empty to use default (~/.reticulum)
configdir =

[client]
# Path to persist this client's identity
identity_path = ./rproxy_client.id

# Seconds to wait for a link to be established
link_timeout = 15

# Seconds to wait for a response from the proxy
request_timeout = 60

# Forget a proxy if not re-announced within this many seconds (0 = never)
proxy_ttl = 900

[webapp]
host  = 127.0.0.1
port  = 8585
debug = false
"""

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [CLIENT]  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rproxy.client")


# ─── Shared state (thread-safe) ───────────────────────────────────────────────
proxy_registry: dict[str, dict] = {}  # hash_hex → proxy info dict
registry_lock  = threading.Lock()

link_cache: dict[str, RNS.Link] = {}   # hash_hex → active RNS.Link
link_lock  = threading.Lock()


# ─── Proxy persistence ────────────────────────────────────────────────────────
STORE_PATH: str = "./rproxy_known_proxies.json"

def _registry_to_json() -> list:
    with registry_lock:
        return [
            {k: v for k, v in info.items() if k != "identity"}
            for info in proxy_registry.values()
        ]

def save_registry():
    try:
        with open(STORE_PATH, "w") as f:
            json.dump(_registry_to_json(), f, indent=2)
        log.debug(f"Registry saved  ({len(proxy_registry)} proxies)")
    except Exception as e:
        log.warning(f"Could not save registry: {e}")

def load_registry():
    """Load persisted proxies. Tries to recall RNS identity for each."""
    if not os.path.exists(STORE_PATH):
        return
    try:
        with open(STORE_PATH) as f:
            entries = json.load(f)
    except Exception as e:
        log.warning(f"Could not load registry: {e}")
        return

    loaded = 0
    for entry in entries:
        hx = entry.get("hash", "")
        if not hx:
            continue
        try:
            identity = RNS.Identity.recall(bytes.fromhex(hx))
        except Exception:
            identity = None

        with registry_lock:
            proxy_registry[hx] = {
                "hash":           hx,
                "display_name":   entry.get("display_name", f"rProxy-{hx[-6:]}"),
                "identity":       identity,
                "first_seen":     entry.get("first_seen"),
                "last_seen":      entry.get("last_seen"),
                "announce_count": entry.get("announce_count", 0),
                "status":         "unknown",
                "last_ping":      None,
                "favorite":       entry.get("favorite", False),
            }
        loaded += 1

    log.info(f"Loaded {loaded} known proxies from {STORE_PATH}")


# ─── Announce handler ─────────────────────────────────────────────────────────
class ProxyAnnounceHandler:
    """Filters announces to rproxy.service and registers discovered proxies."""

    def __init__(self, ttl: int = 0):
        self.aspect_filter = f"{APP_NAME}.{ASPECT}"
        self.ttl = ttl

    def received_announce(self, destination_hash, announced_identity, app_data):
        hash_hex = destination_hash.hex()

        if app_data:
            try:
                display_name = app_data.decode("utf-8").strip()
            except Exception:
                display_name = f"rProxy-{hash_hex[-6:]}"
        else:
            display_name = f"rProxy-{hash_hex[-6:]}"

        now = datetime.now(timezone.utc).isoformat()

        with registry_lock:
            existing = proxy_registry.get(hash_hex, {})
            proxy_registry[hash_hex] = {
                "hash":           hash_hex,
                "display_name":   display_name,
                "identity":       announced_identity,
                "first_seen":     existing.get("first_seen", now),
                "last_seen":      now,
                "announce_count": existing.get("announce_count", 0) + 1,
                # An announce arriving = node is reachable right now
                "status":         "online",
                "last_ping":      now,
                "favorite":       existing.get("favorite", False),
            }

        log.info(f"Proxy announced  →  {display_name}  ({hash_hex[-12:]}…)")
        save_registry()

    def expire_stale(self):
        """Remove proxies not seen within TTL seconds."""
        if self.ttl <= 0:
            return
        cutoff = time.time() - self.ttl
        to_remove = []
        with registry_lock:
            for hx, info in proxy_registry.items():
                try:
                    seen = datetime.fromisoformat(info["last_seen"]).timestamp()
                    if seen < cutoff:
                        to_remove.append(hx)
                except Exception:
                    pass
            for hx in to_remove:
                log.info(f"Proxy expired  →  {proxy_registry[hx]['display_name']}")
                del proxy_registry[hx]
                link_cache.pop(hx, None)


# ─── RNS Client ───────────────────────────────────────────────────────────────
# ─── Bookmarks persistence ────────────────────────────────────────────────────
BOOKMARKS_PATH: str = "./rproxy_bookmarks.json"
bookmarks: list = []
bookmarks_lock = threading.Lock()

def save_bookmarks():
    try:
        with bookmarks_lock:
            with open(BOOKMARKS_PATH, "w") as f:
                json.dump(bookmarks, f, indent=2)
    except Exception as e:
        log.warning(f"Could not save bookmarks: {e}")

def load_bookmarks():
    global bookmarks
    if not os.path.exists(BOOKMARKS_PATH):
        return
    try:
        with open(BOOKMARKS_PATH) as f:
            with bookmarks_lock:
                bookmarks[:] = json.load(f)
        log.info(f"Loaded {len(bookmarks)} bookmarks from {BOOKMARKS_PATH}")
    except Exception as e:
        log.warning(f"Could not load bookmarks: {e}")


class RProxyClient:
    def __init__(self, config_path: str = CONFIG_FILE):
        self.cfg = configparser.ConfigParser()
        self.cfg.read_string(DEFAULT_CONFIG)
        if os.path.exists(config_path):
            self.cfg.read(config_path)

        rns_dir = self.cfg.get("reticulum", "configdir", fallback="").strip() or None
        self.reticulum = RNS.Reticulum(configdir=rns_dir)

        id_path = self.cfg.get("client", "identity_path", fallback="./rproxy_client.id")
        if os.path.exists(id_path):
            self.identity = RNS.Identity.from_file(id_path)
            log.info(f"Identity loaded  →  {id_path}")
        else:
            self.identity = RNS.Identity()
            self.identity.to_file(id_path)
            log.info(f"New identity created  →  {id_path}")

        self.link_timeout    = self.cfg.getint("client", "link_timeout",    fallback=15)
        self.request_timeout = self.cfg.getint("client", "request_timeout", fallback=60)
        ttl                  = self.cfg.getint("client", "proxy_ttl",       fallback=900)

        self.announce_handler = ProxyAnnounceHandler(ttl=ttl)
        RNS.Transport.register_announce_handler(self.announce_handler)

        # TTL janitor thread
        if ttl > 0:
            def _janitor():
                while True:
                    time.sleep(60)
                    self.announce_handler.expire_stale()
            threading.Thread(target=_janitor, daemon=True).start()

        # Load persisted proxy list before pinger starts
        global STORE_PATH
        STORE_PATH = self.cfg.get("client", "store_path", fallback="./rproxy_known_proxies.json")
        load_registry()

        global BOOKMARKS_PATH
        BOOKMARKS_PATH = self.cfg.get("client", "bookmarks_path", fallback="./rproxy_bookmarks.json")
        load_bookmarks()

        ping_interval = self.cfg.getint("client", "ping_interval", fallback=45)
        self._start_pinger(ping_interval)

        log.info(f"Client identity  →  {self.identity.hash.hex()}")
        log.info(f"Listening for  {APP_NAME}.{ASPECT}  announces…")

    # ── Public API ────────────────────────────────────────────────────────────
    def fetch_via_proxy(self, proxy_hash_hex: str, url: str,
                        method: str = "GET",
                        headers: dict | None = None,
                        body: str | None = None) -> dict:
        """Send an HTTP fetch request through a discovered proxy node."""

        with registry_lock:
            info = proxy_registry.get(proxy_hash_hex)
        if not info:
            return {"ok": False, "error": f"Proxy {proxy_hash_hex[:12]}… not in registry"}

        link = self._get_or_create_link(proxy_hash_hex, info["identity"])
        if link is None:
            return {"ok": False, "error": "Could not establish RNS link to proxy "
                                           f"(timeout={self.link_timeout}s)"}

        payload = json.dumps({
            "method":  method.upper(),
            "url":     url,
            "headers": headers or {},
            "body":    body,
        }).encode("utf-8")

        result: dict = {}
        done = threading.Event()

        def on_response(receipt):
            if receipt.response is not None:
                try:
                    result.update(json.loads(receipt.response.decode("utf-8")))
                except Exception as e:
                    result.update({"ok": False, "error": f"Response parse error: {e}"})
            else:
                result.update({"ok": False, "error": "Empty response from proxy"})
            done.set()

        def on_failed(receipt):
            result.update({"ok": False, "error": "Request failed / timed out on proxy side"})
            done.set()

        link.request(
            "/fetch",
            data=payload,
            response_callback=on_response,
            failed_callback=on_failed,
            timeout=self.request_timeout,
        )

        done.wait(timeout=self.request_timeout + 5)

        if not result:
            # Invalidate cached link
            with link_lock:
                link_cache.pop(proxy_hash_hex, None)
            return {"ok": False, "error": "Client-side timeout — no response received"}

        return result

    # ── Liveness check ───────────────────────────────────────────────────────
    def ping_proxy(self, hash_hex: str) -> str:
        """Try to reach the proxy. Returns 'online', 'offline', or 'unknown'."""
        with registry_lock:
            info = proxy_registry.get(hash_hex)
        if not info:
            return "unknown"

        # Fast path: already have an active cached link
        with link_lock:
            existing = link_cache.get(hash_hex)
        if existing is not None and existing.status == RNS.Link.ACTIVE:
            status = "online"
        else:
            # Try to establish a link with a short timeout
            link = self._get_or_create_link(hash_hex, info["identity"], timeout_override=8)
            status = "online" if (link is not None and link.status == RNS.Link.ACTIVE) else "offline"

        now = datetime.now(timezone.utc).isoformat()
        with registry_lock:
            if hash_hex in proxy_registry:
                proxy_registry[hash_hex]["status"]    = status
                proxy_registry[hash_hex]["last_ping"] = now
        log.info(f"Ping  {info['display_name']}  →  {status}")
        return status

    def _start_pinger(self, interval: int = 45):
        """Background thread: ping all known proxies every `interval` seconds."""
        def _loop():
            # Initial delay so RNS has time to settle
            time.sleep(10)
            while True:
                hashes = list(proxy_registry.keys())
                for hx in hashes:
                    try:
                        self.ping_proxy(hx)
                    except Exception as e:
                        log.warning(f"Pinger error for {hx[-8:]}: {e}")
                time.sleep(interval)
        t = threading.Thread(target=_loop, daemon=True, name="rproxy-pinger")
        t.start()
        log.info(f"Background pinger started (every {interval}s)")

    # ── Link management ───────────────────────────────────────────────────────
    def _get_or_create_link(self, hash_hex: str, identity: RNS.Identity, timeout_override: int | None = None) -> RNS.Link | None:
        with link_lock:
            existing = link_cache.get(hash_hex)
            if existing is not None and existing.status == RNS.Link.ACTIVE:
                log.debug(f"Reusing cached link to {hash_hex[-12:]}")
                return existing
            # Remove stale entry
            if existing is not None:
                link_cache.pop(hash_hex, None)

        # Reconstruct the destination from the announced identity
        dest = RNS.Destination(
            identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            APP_NAME,
            ASPECT,
        )

        established_event = threading.Event()
        link_holder: dict = {}

        def on_established(link):
            link_holder["link"] = link
            established_event.set()

        def on_failed(link):
            established_event.set()

        log.info(f"Establishing link to {hash_hex[-12:]}…")
        RNS.Link(dest, established_callback=on_established, closed_callback=on_failed)

        t = timeout_override if timeout_override is not None else self.link_timeout
        established_event.wait(timeout=t)

        link = link_holder.get("link")
        if link and link.status == RNS.Link.ACTIVE:
            with link_lock:
                link_cache[hash_hex] = link
            log.info(f"Link established  →  {hash_hex[-12:]}")
            return link

        log.warning(f"Failed to establish link to {hash_hex[-12:]}")
        return None


# ─── Flask app ────────────────────────────────────────────────────────────────
def create_app(client: RProxyClient) -> Flask:
    app = Flask(__name__)

    # ── Main UI ───────────────────────────────────────────────────────────────
    @app.route("/")
    def index():
        return render_template("index.html")

    # ── REST: list proxies ────────────────────────────────────────────────────
    @app.route("/api/proxies")
    def api_proxies():
        try:
            with registry_lock:
                result = [
                    {k: v for k, v in info.items() if k != "identity"}
                    for info in proxy_registry.values()
                ]
            result.sort(key=lambda x: x.get("last_seen") or "", reverse=True)
            return jsonify({"proxies": result})
        except Exception as e:
            log.error(f"api_proxies error: {e}", exc_info=True)
            return jsonify({"proxies": [], "error": str(e)}), 200

    # ── DEBUG: raw registry dump ──────────────────────────────────────────────
    @app.route("/api/debug")
    def api_debug():
        with registry_lock:
            dump = {k: {dk: str(dv) for dk, dv in info.items()} for k, info in proxy_registry.items()}
        return jsonify({"count": len(dump), "registry": dump})

    # ── REST: remove proxy from registry ─────────────────────────────────────
    @app.route("/api/proxies/<hash_hex>", methods=["DELETE"])
    def api_remove_proxy(hash_hex: str):
        with registry_lock:
            removed = proxy_registry.pop(hash_hex, None)
        with link_lock:
            link_cache.pop(hash_hex, None)
        if removed:
            save_registry()
            return jsonify({"ok": True, "removed": hash_hex})
        abort(404)

    # ── REST: fetch via proxy ─────────────────────────────────────────────────
    @app.route("/api/fetch", methods=["POST"])
    def api_fetch():
        body = freq.get_json(force=True, silent=True)
        if not body:
            return jsonify({"ok": False, "error": "No JSON body"}), 400

        proxy_hash = body.get("proxy_hash", "").strip()
        url        = body.get("url",        "").strip()
        method     = body.get("method",     "GET").upper()
        headers    = body.get("headers",    {})
        req_body   = body.get("body",       None)

        if not proxy_hash:
            return jsonify({"ok": False, "error": "proxy_hash is required"}), 400
        if not url:
            return jsonify({"ok": False, "error": "url is required"}), 400

        t0     = time.time()
        result = client.fetch_via_proxy(proxy_hash, url, method, headers, req_body)
        result["elapsed_ms"] = round((time.time() - t0) * 1000)
        return jsonify(result)

    # ── REST: toggle proxy favorite ───────────────────────────────────────────
    @app.route("/api/proxies/<hash_hex>/favorite", methods=["POST"])
    def api_toggle_favorite(hash_hex: str):
        with registry_lock:
            info = proxy_registry.get(hash_hex)
            if not info:
                abort(404)
            info["favorite"] = not info.get("favorite", False)
            new_state = info["favorite"]
        save_registry()
        return jsonify({"ok": True, "hash": hash_hex, "favorite": new_state})

    # ── REST: bookmarks ────────────────────────────────────────────────────────
    @app.route("/api/bookmarks")
    def api_list_bookmarks():
        with bookmarks_lock:
            return jsonify({"bookmarks": list(bookmarks)})

    @app.route("/api/bookmarks", methods=["POST"])
    def api_add_bookmark():
        body = freq.get_json(force=True, silent=True) or {}
        url   = body.get("url",   "").strip()
        title = body.get("title", "").strip() or url
        if not url:
            return jsonify({"ok": False, "error": "url required"}), 400
        import uuid
        entry = {
            "id":       str(uuid.uuid4())[:8],
            "url":      url,
            "title":    title,
            "added_at": datetime.now(timezone.utc).isoformat(),
        }
        with bookmarks_lock:
            # Avoid duplicates by URL
            if any(b["url"] == url for b in bookmarks):
                return jsonify({"ok": False, "error": "already bookmarked"}), 409
            bookmarks.append(entry)
        save_bookmarks()
        return jsonify({"ok": True, "bookmark": entry})

    @app.route("/api/bookmarks/<bk_id>", methods=["DELETE"])
    def api_delete_bookmark(bk_id: str):
        with bookmarks_lock:
            before = len(bookmarks)
            bookmarks[:] = [b for b in bookmarks if b["id"] != bk_id]
            removed = len(bookmarks) < before
        if removed:
            save_bookmarks()
            return jsonify({"ok": True})
        abort(404)

    @app.route("/api/bookmarks/<bk_id>", methods=["PATCH"])
    def api_rename_bookmark(bk_id: str):
        body  = freq.get_json(force=True, silent=True) or {}
        title = body.get("title", "").strip()
        if not title:
            return jsonify({"ok": False, "error": "title required"}), 400
        with bookmarks_lock:
            for b in bookmarks:
                if b["id"] == bk_id:
                    b["title"] = title
                    save_bookmarks()
                    return jsonify({"ok": True, "bookmark": b})
        abort(404)

    # ── REST: ping a proxy ────────────────────────────────────────────────────
    @app.route("/api/ping/<hash_hex>", methods=["POST"])
    def api_ping(hash_hex: str):
        status = client.ping_proxy(hash_hex)
        return jsonify({"ok": True, "hash": hash_hex, "status": status})

    # ── REST: client identity ─────────────────────────────────────────────────
    @app.route("/api/identity")
    def api_identity():
        return jsonify({
            "hash": client.identity.hash.hex(),
            "hash_short": client.identity.hash.hex()[-12:],
        })

    return app


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="rProxy Client WebApp")
    parser.add_argument("--config", default=CONFIG_FILE, metavar="FILE")
    parser.add_argument("--generate-config", action="store_true",
                        help="Write a default config file and exit")
    args = parser.parse_args()

    if args.generate_config:
        with open(args.config, "w") as f:
            f.write(DEFAULT_CONFIG.strip() + "\n")
        print(f"Config written to {args.config}")
        sys.exit(0)

    rpc = RProxyClient(args.config)

    cfg = configparser.ConfigParser()
    cfg.read_string(DEFAULT_CONFIG)
    if os.path.exists(args.config):
        cfg.read(args.config)

    host  = cfg.get("webapp", "host",  fallback="127.0.0.1")
    port  = cfg.getint("webapp", "port",  fallback=8585)
    debug = cfg.getboolean("webapp", "debug", fallback=False)

    flask_app = create_app(rpc)

    log.info(f"WebApp  →  http://{host}:{port}/")
    try:
        flask_app.run(host=host, port=port, debug=debug, use_reloader=False)
    except KeyboardInterrupt:
        log.info("Shutting down.")
