# rProxy - v0.1 (Initial Release)
Reticulum HTTP/HTTPS Proxy Server & WebUI Client/Browser

---

**rProxy** is a decentralized HTTP proxy system built on the [Reticulum Network Stack](https://reticulum.network/). 

Proxy nodes announce themselves over the mesh network with a custom announce (not visible on the lxmf peer list; only the client will see it!) 

Clients discover them automatically, pick an exit node, and browse the web through it, all routed over Reticulum.

---

```
Working scheme:

[Browser / WebApp]  -- RNS Link --►  [rProxy Server]  --HTTP/HTTPS--►  [Internet]
     Client                            Exit Node
```

---

## Features

### Server (`rproxy_server.py`)
- Announces itself on the Reticulum network with a unique aspect (`rproxy.service`)
- Auto-generated display name from hash suffix - `rProxy-a1c2d5`
- Configurable announce interval
- Forwards HTTP/HTTPS requests received via RNS link
- Auto-detects response charset from `Content-Type` header
- gzip / deflate / brotli decompression via `decode_content=True`
- Configurable allowed methods, request timeout, max response size
- Optional SSL verification bypass (`verify_ssl = false`) for Windows environments
- Optional upstream HTTP proxy support

### Client WebApp (`rproxy_client_webapp.py`)
- Flask web UI accessible at `http://127.0.0.1:8585`
- Listens **only** for `rproxy.service` announces — ignores all other RNS traffic
- **Proxy registry** with persistent storage (`rproxy_known_proxies.json`)
  - Nodes loaded on startup, no need to wait for the next announce
  - Deleted manually with the trash button; never auto-removed
- **Background liveness pinger** — checks all known proxies periodically for online/offline state
- **Favorite proxies** (⭐) — starred nodes always shown at the top
- **Bookmark bar** — save URLs with auto-title detection, chips for quick access, full dropdown panel with rename/delete
- **Debug endpoint** at `/api/debug` — inspect raw registry state

### Browser UI
- Dark-themed single-page app
- **Exit Nodes sidebar** - live status dots (online/offline/checking), full hash display, announce count, last ping time
- **Request builder** - method selector, URL bar, custom JSON headers
- **Auto-scheme**: type `example.com`, get `https://` first, fallback to `http://` automatically
- **Response tabs**: Body (JSON pretty-print), Headers, Raw JSON, **Preview**
- **Preview tab** (HTML responses):
  - Renders page in sandboxed `<iframe>` with injected `<base href>` for correct relative URL resolution
  - **Intercepts all link clicks and form submissions** — navigation stays inside the proxy
  - **Back / Forward** history navigation
  - Mini toolbar with reload and URL display
  - Auto-selected when response is HTML
- **Bookmark bar** — click any saved URL to load it immediately through the proxy

---

## Files structure:

```
rProxy/
├── rproxy_server.py          # Proxy server node
├── rproxy_server.ini         # Server configuration
├── rproxy_client_webapp.py   # Client web application
├── rproxy_client.ini         # Client configuration
└── templates/
    └── index.html            # Browser UI
```

> **All five files are required.** The `templates/` folder must be in the same directory as `rproxy_client_webapp.py`.

---

## Quick Start

### Requirements

```bash
pip install rns requests flask
```

Python 3.10+ recommended. Reticulum must be configured and running (at least one interface connected).

---

### 1. Start the Proxy Server

```bash
python rproxy_server.py --config rproxy_server.ini
or just:
python rproxy_server.py
```

First run creates a persistent identity file (`rproxy_server.id`) and announces immediately:

```
13:29:11  [SERVER]  INFO      Display name  :  rProxy-53f7aa
13:29:11  [SERVER]  INFO      Full hash     :  666bf48e2b6a60db95b11bb7d453f7aa
13:29:11  [SERVER]  INFO      Aspect        :  rproxy.service
13:29:11  [SERVER]  INFO      Announce      :  every 300s
13:29:11  [SERVER]  INFO      Announced as  rProxy-53f7aa
```

**Windows SSL note:** if you get `CERTIFICATE_VERIFY_FAILED`, add this to `rproxy_server.ini`:
```ini
verify_ssl = false (set by default, edit if needed)
```

---

### 2. Start the Client WebApp

```bash
python rproxy_client_webapp.py --config rproxy_client.ini
or just:
python rproxy_client_webapp.py
```

Open your browser at **`http://127.0.0.1:8585`**

```
14:21:57  [CLIENT]  INFO      Identity loaded  →  ./rproxy_client.id
14:21:57  [CLIENT]  INFO      Loaded 1 known proxies from ./rproxy_known_proxies.json
14:21:57  [CLIENT]  INFO      Background pinger started (every 45s)
14:21:57  [CLIENT]  INFO      Listening for rproxy.service announces…
```

---

### 3. Browse

1. Wait for proxy nodes to appear in the **Exit Nodes** sidebar (or they load instantly from saved list)
2. Click a node to select it as your exit point
3. Type a URL in the request bar and press **Send** or **Enter**
4. HTML pages open in the **Preview** tab — click links to navigate through the proxy

---

## Configuration

### `rproxy_server.ini`

```ini
[reticulum]
configdir =                    # Leave empty for default (~/.reticulum)

[server]
identity_path = ./rproxy_server.id
announce_interval = 300        # Seconds between network announces
request_timeout = 30           # HTTP fetch timeout (seconds)
max_response_size = 4194304    # Max relay size in bytes (default 4 MB)
allowed_methods = GET,HEAD,POST,PUT,DELETE
# verify_ssl = false           # Uncomment on Windows if SSL errors occur
# http_proxy = http://proxy:3128  # Optional upstream proxy
```

### `rproxy_client.ini`

```ini
[reticulum]
configdir =

[client]
identity_path = ./rproxy_client.id
link_timeout = 15              # Seconds to wait for RNS link establishment
request_timeout = 60           # Seconds to wait for proxy response
proxy_ttl = 900                # Remove unseen proxies after N seconds (0 = never)
ping_interval = 45             # Background liveness check interval (seconds)
store_path = ./rproxy_known_proxies.json
bookmarks_path = ./rproxy_bookmarks.json

[webapp]
host = 127.0.0.1
port = 8585
debug = false
```

---

## How It Works

```
Client                          Reticulum Network              Server
  │                                                               │
  │  ← Announce (rproxy.service, display_name, hash) ────────── │
  │                                                               │
  │  ── RNS Link establishment ──────────────────────────────► │
  │                                                               │
  │  ── /fetch  {method, url, headers} ──────────────────────► │
  │                                   ┌─────────────────────────┘
  │                                   │  HTTP/HTTPS request to internet
  │                                   │  ← response
  │                                   └─────────────────────────┐
  │  ◄─ {status, headers, body, url} ────────────────────────── │
  │                                                               │
  └─ Render in Preview iframe                                     │
```

- The **announce** carries the display name as `app_data` - the client reads it without establishing a connection
- **RNS links** are cached and reused for subsequent requests to the same node
- The **interceptor script** injected into preview pages catches all clicks/submits and sends them back to the parent window via `postMessage`, keeping all navigation inside the proxy

---

## REST API

The client webapp exposes a simple REST API:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/proxies` | List all known proxy nodes |
| `DELETE` | `/api/proxies/<hash>` | Remove a proxy from the list |
| `POST` | `/api/proxies/<hash>/favorite` | Toggle favorite status |
| `POST` | `/api/ping/<hash>` | Manual liveness check |
| `POST` | `/api/fetch` | Fetch a URL through a proxy node |
| `GET` | `/api/bookmarks` | List bookmarks |
| `POST` | `/api/bookmarks` | Add a bookmark |
| `DELETE` | `/api/bookmarks/<id>` | Delete a bookmark |
| `PATCH` | `/api/bookmarks/<id>` | Rename a bookmark |
| `GET` | `/api/identity` | Client identity hash |
| `GET` | `/api/debug` | Raw registry dump (debug) |

---

## Limitations

This script is just a working proof of concept on what can be done using the Reticulum APIs.

- Sites with `X-Frame-Options: SAMEORIGIN` or `Content-Security-Policy: frame-ancestors` will refuse to render in the preview iframe (e.g. Google, Facebook). This is enforced by the browser and cannot be bypassed client-side.
- Binary responses (images, PDFs, downloads) are not yet handled as files - only HTML and text content is rendered usefully.
- POST forms work but multipart file uploads are not supported.
- RNS link MTU limits apply — very large responses are truncated at `max_response_size`.

---

## Author

**fr33n0w** — [github.com/fr33n0w](https://github.com/fr33n0w)

Built for the [Reticulum](https://reticulum.network/) / [LXMF](https://github.com/markqvist/LXMF) / [NomadNet](https://github.com/markqvist/NomadNet) ecosystem.
