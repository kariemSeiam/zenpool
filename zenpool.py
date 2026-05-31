#!/usr/bin/env python3
"""zenpool — Unified API key pool & distributed proxy for OpenCode Zen.

One file, two modes:
  python3 zenpool.py hub         → Run the central hub (on your main server)
  python3 zenpool.py node        → Run a node agent (on any device)
  python3 zenpool.py node --hub http://x:5051  → Connect to specific hub

Install: curl -fsSL https://<your-host>/zenpool.py | python3 - node
"""
import json
import os
import platform
import threading
import time
import uuid
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from hashlib import sha256

# ─── Config ──────────────────────────────────────────────────────────

VERSION = "1.0.0"
HUB_PORT = int(os.environ.get("ZENPOOL_PORT", 5051))
NODE_PORT = int(os.environ.get("ZENPOOL_NODE_PORT", 5052))
DATA_FILE = os.environ.get("ZENPOOL_DATA", "zenpool-data.json")
HEARTBEAT_INTERVAL = 30
ZEN_API = "https://opencode.ai/zen/v1/chat/completions"


# ═══════════════════════════════════════════════════════════════════════
#  HUB
# ═══════════════════════════════════════════════════════════════════════

class KeyPool:
    """Central key pool with round-robin + rate-limit backoff."""

    def __init__(self):
        self.keys = {}
        self.nodes = {}
        self._rr = 0
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        try:
            with open(DATA_FILE) as f:
                d = json.load(f)
                self.keys = d.get("keys", {})
                self.nodes = d.get("nodes", {})
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save(self):
        with open(DATA_FILE, "w") as f:
            json.dump({"keys": self.keys, "nodes": self.nodes}, f, indent=2)

    def add_key(self, value, label=""):
        kid = sha256(value.encode()).hexdigest()[:12]
        with self._lock:
            if kid not in self.keys:
                self.keys[kid] = {"key": value, "label": label or kid[:8], "active": True,
                                  "cool_until": 0, "total": 0, "errors": 0}
                self._save()
            return kid

    def remove_key(self, kid):
        with self._lock:
            self.keys.pop(kid, None)
            self._save()

    def get_key(self):
        """Round-robin across non-cooled keys."""
        with self._lock:
            now = time.time()
            active = [k for k, v in self.keys.items() if v["active"] and v["cool_until"] < now]
            if not active:
                return None
            self._rr = (self._rr + 1) % len(active)
            kid = active[self._rr]
            k = self.keys[kid]
            k["total"] += 1
            k["last_used"] = now
            return {"id": kid, "key": k["key"], "label": k["label"]}

    def report_error(self, kid):
        with self._lock:
            k = self.keys.get(kid)
            if not k:
                return
            k["errors"] += 1
            backoff = min(300 * (2 ** min(k["errors"] - 1, 4)), 3600)
            k["cool_until"] = time.time() + backoff
            if k["errors"] >= 10:
                k["active"] = False

    def report_ok(self, kid):
        with self._lock:
            k = self.keys.get(kid)
            if k:
                k["errors"] = max(0, k["errors"] - 1)

    def list_keys(self):
        with self._lock:
            now = time.time()
            return {kid: {k: v for k, v in kv.items() if k != "key"} | {
                "cool": kv["cool_until"] > now,
                "cool_remaining": max(0, int(kv["cool_until"] - now))
            } for kid, kv in self.keys.items()}

    def register_node(self, name, ip, device="unknown"):
        nid = str(uuid.uuid4())[:8]
        with self._lock:
            self.nodes[nid] = {"name": name, "ip": ip, "device": device, "seen": time.time()}
            self._save()
        return nid

    def heartbeat(self, nid):
        with self._lock:
            if nid in self.nodes:
                self.nodes[nid]["seen"] = time.time()

    def list_nodes(self):
        with self._lock:
            now = time.time()
            return {nid: n | {"online": now - n["seen"] < 60}
                    for nid, n in self.nodes.items()}

    def prune(self, timeout=90):
        with self._lock:
            now = time.time()
            dead = [nid for nid, n in self.nodes.items() if now - n["seen"] > timeout]
            for nid in dead:
                del self.nodes[nid]


# ─── Hub HTTP Server ─────────────────────────────────────────────────

def run_hub():
    pool = KeyPool()

    class HubHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _s(self, data, code=200):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def _e(self, msg, code=400):
            self._s({"error": msg}, code)

        def _body(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n)) if n else {}

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.end_headers()

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/health":
                self._s({"ok": True, "host": platform.node(), "keys": len(pool.keys), "nodes": len(pool.nodes)})
            elif p == "/keys":
                self._s({"keys": pool.list_keys()})
            elif p == "/nodes":
                self._s({"nodes": pool.list_nodes()})
            else:
                self._e("not found", 404)

        def do_POST(self):
            p = self.path.split("?")[0]
            try:
                b = self._body()
            except Exception:
                return self._e("bad json")
            if p == "/keys":
                if not b.get("key"):
                    return self._e("missing key")
                kid = pool.add_key(b["key"], b.get("label", ""))
                self._s({"id": kid})
            elif p == "/register":
                nid = pool.register_node(b.get("name", f"n-{len(pool.nodes)+1}"), self.client_address[0], b.get("device"))
                self._s({"node_id": nid, "interval": HEARTBEAT_INTERVAL})
            elif p == "/heartbeat":
                pool.heartbeat(b.get("node_id"))
                self._s({"ok": True})
            elif p == "/next-key":
                k = pool.get_key()
                if k:
                    self._s(k)
                else:
                    self._s({"error": "no keys available"}, 503)
            elif p == "/report":
                ok = b.get("ok", True)
                kid = b.get("key_id")
                if ok:
                    pool.report_ok(kid)
                else:
                    pool.report_error(kid)
                self._s({"ok": True})
            elif p == "/v1/chat/completions":
                self._proxy(b, pool)
            else:
                self._e("not found", 404)

        def do_DELETE(self):
            p = self.path.split("?")[0]
            if p.startswith("/keys/"):
                pool.remove_key(p.split("/")[-1])
                self._s({"ok": True})
            else:
                self._e("not found", 404)

        def _proxy(self, body, pool):
            k = pool.get_key()
            if not k:
                return self._e("no keys available", 503)
            req = urllib.request.Request(
                ZEN_API,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {k['key']}",
                         "User-Agent": "curl/7.76.1"}
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    pool.report_ok(k["id"])
                    ctype = r.headers.get("Content-Type", "application/json")
                    self.send_response(r.status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    # Stream chunks for SSE, buffer otherwise
                    if "text/event-stream" in ctype:
                        while True:
                            chunk = r.read(4096)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    else:
                        self.wfile.write(r.read())
            except urllib.error.HTTPError as e:
                pool.report_error(k["id"])
                try:
                    data = e.read()
                    # OpenCode sometimes returns non-JSON errors
                    body = json.loads(data) if data else {"error": f"HTTP {e.code}"}
                except (json.JSONDecodeError, TypeError):
                    body = {"error": f"upstream HTTP {e.code}", "detail": data.decode(errors='replace')[:200]}
                self._s(body, e.code)
            except urllib.error.URLError as e:
                self._e(f"upstream unreachable: {e.reason}", 502)
            except Exception as e:
                self._e(f"proxy error: {e}", 502)

    def _prune():
        while True:
            time.sleep(15)
            pool.prune()

    threading.Thread(target=_prune, daemon=True).start()
    print(f"\n  🐍 ZenPool Hub v{VERSION}  —  {platform.node()}")
    print(f"  ├─ Port: {HUB_PORT}")
    print(f"  ├─ Keys: {len(pool.keys)}")
    print(f"  └─ Data: {os.path.abspath(DATA_FILE)}")
    print("\n  Endpoints:")
    print("    GET  /health  POST /keys  GET /keys  DELETE /keys/<id>")
    print("    POST /register  POST /heartbeat  POST /next-key  POST /report")
    print("    POST /v1/chat/completions  (direct proxy)")
    print(f"\n  Deploy nodes: curl -fsSL <url> | python3 - node --hub http://<this-ip>:{HUB_PORT}")
    print()
    ThreadingHTTPServer(("0.0.0.0", HUB_PORT), HubHandler).serve_forever()


# ═══════════════════════════════════════════════════════════════════════
#  NODE
# ═══════════════════════════════════════════════════════════════════════

class NodeClient:
    def __init__(self, hub, local_key=None):
        self.hub = hub.rstrip("/")
        self.local_key = local_key
        self.nid = None
        self.name = platform.node() or "unknown"
        self.device = f"{platform.system()}/{platform.machine()}"

    def _call(self, path, data=None):
        body = json.dumps(data).encode() if data else None
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(f"{self.hub}{path}", data=body,
                                       headers={"Content-Type": "application/json"}),
                timeout=10
            )
            return json.loads(r.read())
        except Exception as e:
            return {"error": str(e)}

    def register(self):
        r = self._call("/register", {"name": self.name, "device": self.device})
        if r.get("node_id"):
            self.nid = r["node_id"]
            return True
        return False

    def heartbeat(self):
        self._call("/heartbeat", {"node_id": self.nid})

    def next_key(self):
        r = self._call("/next-key", {"node_id": self.nid})
        return r if r.get("key") else None

    def report(self, kid, ok=True):
        self._call("/report", {"key_id": kid, "ok": ok, "node_id": self.nid})


def run_node(hub_url, local_key=None):
    client = NodeClient(hub_url, local_key=local_key)

    class NodeHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _s(self, data, code=200):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def _e(self, msg, code=400):
            self._s({"error": msg}, code)

        def _body(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n)) if n else {}

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.end_headers()

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/health":
                self._s({"ok": True, "node": client.nid, "hub": client.hub})
            elif p == "/models":
                self._s({"object": "list", "data": [
                    {"id": "deepseek-v4-flash-free"}, {"id": "nemotron-3-super-free"},
                    {"id": "mimo-v2.5-free"}, {"id": "big-pickle"},
                ]})
            else:
                self._e("not found", 404)

        def do_POST(self):
            p = self.path.split("?")[0]
            try:
                b = self._body()
            except Exception:
                return self._e("bad json")
            if p == "/v1/chat/completions":
                self._proxy(b)
            else:
                self._e("not found", 404)

        def _proxy(self, body):
            # Use local key if set, otherwise ask the hub
            if client.local_key:
                key = {"key": client.local_key, "id": None}
            else:
                key = client.next_key()
            headers = {"Content-Type": "application/json", "User-Agent": "curl/7.76.1"}
            if key:
                headers["Authorization"] = f"Bearer {key['key']}"
            req = urllib.request.Request(
                ZEN_API, data=json.dumps(body).encode(), headers=headers
            )
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = r.read()
                    if key and key["id"]:
                        client.report(key["id"], ok=True)
                    status = r.status
                    ctype = r.headers.get("Content-Type", "application/json")
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
            except urllib.error.HTTPError as e:
                if key and key["id"]:
                    client.report(key["id"], ok=False)
                data = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._e(str(e), 502)

    # Register + heartbeat
    def _loop():
        while True:
            if not client.nid:
                if client.register():
                    print(f"  ✅ Connected to hub: {client.nid}")
            else:
                client.heartbeat()
            time.sleep(HEARTBEAT_INTERVAL)

    print(f"\n  🐍 ZenPool Node v{VERSION}  —  {client.name}")
    print(f"  ├─ Hub: {client.hub}")
    print(f"  ├─ Port: {NODE_PORT}")
    print(f"  └─ Device: {client.device}")
    print()
    client.register()
    threading.Thread(target=_loop, daemon=True).start()

    print(f"  🚀 Proxy ready: http://localhost:{NODE_PORT}/v1/chat/completions")
    print("  Set your code to use this URL + any key (key is ignored)")
    print()
    ThreadingHTTPServer(("0.0.0.0", NODE_PORT), NodeHandler).serve_forever()


# ═══════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(prog="zenpool", description="ZenPool — distributed key proxy for OpenCode")
    p.add_argument("mode", choices=["hub", "node"], help="run as hub (server) or node (agent)")
    p.add_argument("--hub", default="http://localhost:5051", help="hub URL (for node mode)")
    p.add_argument("--key", default=None, help="local API key (node runs standalone, no hub needed)")

    args = p.parse_args()

    print(r"""
            ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
            ██ ▄▄ █ ▄▄▀█ ▄▄▀█ ▄▄▀█ ▄▄▄█▄ ▄▄▄██
            ██ █  █ ▀▀ █ ▀▀ █ ▀▀ █ █▀▀██ █ ███
            ██ █▄▄█ ██▄▀▄██▄▀▄██▄▀▄▄▄▄██ █ ███
            ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
            """)

    if args.mode == "hub":
        run_hub()
    else:
        run_node(args.hub, local_key=args.key)
