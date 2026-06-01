#!/usr/bin/env python3
"""zenpool — Unified API key pool & distributed proxy for OpenCode Zen.

One file, two modes:
  python3 zenpool.py hub         → Run the central hub (on your main server)
  python3 zenpool.py node        → Run a node agent (on any device)
  python3 zenpool.py node --hub http://x:5051  → Connect to specific hub

Install: curl -fsSL https://<your-host>/zenpool.py | python3 - node
Windows (PowerShell):
  (Invoke-WebRequest -Uri https://<your-host>/zenpool.py).Content | python - node
"""
import json
import os
import platform
import signal
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from hashlib import sha256

# ─── Config ──────────────────────────────────────────────────────────

VERSION = "2.1.10"
DEFAULT_HUB = os.environ.get("ZENPOOL_HUB", "https://srv880434.hstgr.cloud")
HUB_PORT = int(os.environ.get("ZENPOOL_PORT", 5051))
NODE_PORT = int(os.environ.get("ZENPOOL_NODE_PORT", 5052))
DATA_FILE = os.environ.get("ZENPOOL_DATA", "zenpool-data.json")
HEARTBEAT_INTERVAL = 30
POLL_INTERVAL = 1
WORK_TIMEOUT = 120
PUSH_TIMEOUT = 4
PULL_TIMEOUT = 90
MAX_BODY = int(os.environ.get("ZENPOOL_MAX_BODY", 10485760))
AUTH_SECRET = os.environ.get("ZENPOOL_SECRET", "")
ZEN_API = "https://opencode.ai/zen/v1/chat/completions"


# ─── Cross-platform state directory ──────────────────────────────────

def _default_state_dir():
    """Return platform-appropriate state directory for zenpool.

    Windows: %LOCALAPPDATA%/zenpool
    macOS:   ~/Library/Application Support/zenpool
    Linux:   $XDG_DATA_HOME/zenpool or ~/.local/share/zenpool
    """
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA",
                              os.path.join(os.path.expanduser("~"), "AppData", "Local"))
        return os.path.join(base, "zenpool")
    if system == "Darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "zenpool")
    # Linux / BSD / others
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return os.path.join(xdg, "zenpool")
    return os.path.join(os.path.expanduser("~"), ".local", "share", "zenpool")


# ─── Graceful shutdown ───────────────────────────────────────────────

shutting_down = False
_work_queue_for_shutdown = None  # set by run_hub after WorkQueue creation


def _handle_signal(signum, frame):
    """SIGTERM/SIGINT handler: set flag, cancel pending work, exit."""
    global shutting_down
    if shutting_down:
        # Second signal = force exit
        sys.exit(1)
    shutting_down = True
    name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
    print(f"\n  ␡ {name} received — shutting down gracefully...")
    wq = _work_queue_for_shutdown
    if wq is not None:
        # Cancel all pending work items
        with wq._lock:
            for req_id, item in list(wq._pending.items()):
                item["event"].set()
            wq._pending.clear()
        print(f"  ✓ Cancelled pending work items")
    sys.exit(0)


# Register signal handlers (best-effort on platforms without signal)
try:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
except (AttributeError, ValueError, OSError):
    # Windows doesn't have SIGTERM in some Python builds;
    # ValueError if called from non-main thread.
    pass


# ─── Connection cache ──────────────────────────────────────────────────

class ConnectionCache:
    """Reuse HTTP openers per host:port for keep-alive connection pooling.

    Stores (opener, last_used) tuples keyed by host:port.  Evicts entries
    when their age exceeds C{max_age} seconds or total entries exceed
    C{max_entries}.
    """

    def __init__(self, max_age=30, max_entries=10):
        self._cache = {}  # "host:port" -> (opener, last_used)
        self._max_age = max_age
        self._max_entries = max_entries
        self._lock = threading.Lock()

    def get_opener(self, host, port):
        """Return a cached opener for host:port, or build a fresh one."""
        key = f"{host}:{port}"
        with self._lock:
            now = time.time()
            entry = self._cache.get(key)
            if entry and (now - entry[1]) < self._max_age:
                self._cache[key] = (entry[0], now)
                return entry[0]
            opener = urllib.request.build_opener(urllib.request.HTTPHandler)
            self._cache[key] = (opener, now)
            self._evict(now)
            return opener

    def _evict(self, now):
        stale = [k for k, v in self._cache.items() if (now - v[1]) > self._max_age]
        for k in stale:
            del self._cache[k]
        if len(self._cache) > self._max_entries:
            sorted_items = sorted(self._cache.items(), key=lambda x: x[1][1])
            for k, _ in sorted_items[:len(self._cache) - self._max_entries]:
                del self._cache[k]


# ═══════════════════════════════════════════════════════════════════════
#  HUB
# ═══════════════════════════════════════════════════════════════════════

class KeyPool:
    """Central key pool with round-robin + rate-limit backoff.
    Falls through to node-contributed keys when local pool is dry.
    """

    def __init__(self):
        self.keys = {}
        self.nodes = {}  # nid -> {name, ip, device, seen, key?}
        self._rr = 0
        self._rr_node = 0
        self._lock = threading.Lock()
        self._start = time.time()
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
        tmp = DATA_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"keys": self.keys, "nodes": self.nodes}, f, indent=2)
            os.replace(tmp, DATA_FILE)
        except OSError as e:
            print(f"  ⚠️  Failed to save data: {e}", file=sys.stderr)

    def add_key(self, value, label=""):
        kid = sha256(value.encode()).hexdigest()[:12]
        with self._lock:
            if kid not in self.keys:
                self.keys[kid] = {"key": value, "label": label or kid[:8], "active": True,
                                  "cool_until": 0, "total": 0, "errors": 0,
                                  "tokens_input": 0, "tokens_output": 0}
                self._save()
            return kid

    def remove_key(self, kid):
        with self._lock:
            self.keys.pop(kid, None)
            self._save()

    def _pick_local_key(self, now):
        """Round-robin across non-cooled local keys. Caller must hold self._lock."""
        active = [k for k, v in self.keys.items() if v["active"] and v["cool_until"] < now]
        if not active:
            return None
        self._rr = (self._rr + 1) % len(active)
        kid = active[self._rr]
        k = self.keys[kid]
        k["total"] += 1
        k["last_used"] = now
        return {"id": kid, "key": k["key"], "label": k["label"], "source": "local"}

    def _pick_any_active_key(self, now):
        """Pick any active key (ignore hub cooldown — for node IP execution)."""
        active = [k for k, v in self.keys.items() if v["active"]]
        if not active:
            return None
        self._rr = (self._rr + 1) % len(active)
        kid = active[self._rr]
        k = self.keys[kid]
        k["total"] += 1
        k["last_used"] = now
        return {"id": kid, "key": k["key"], "label": k["label"], "source": "node-key"}

    def get_key(self):
        """Round-robin across non-cooled keys (local pool only)."""
        with self._lock:
            return self._pick_local_key(time.time())

    def get_key_for_node(self):
        """Key for a node to use from its own IP (ignores hub-IP cooldowns)."""
        with self._lock:
            return self._pick_any_active_key(time.time())

    def _online_nodes(self, now=None):
        now = now or time.time()
        return [(nid, n) for nid, n in self.nodes.items() if now - n.get("seen", 0) < 120]

    def node_online(self, nid):
        with self._lock:
            n = self.nodes.get(nid)
            return bool(n and time.time() - n.get("seen", 0) < 120)

    def _pick_node_route(self, now):
        """Round-robin across online nodes. Caller must hold self._lock."""
        active_nodes = self._online_nodes(now)
        if not active_nodes:
            return None
        self._rr_node = (self._rr_node + 1) % len(active_nodes)
        nid, n = active_nodes[self._rr_node]
        n["total"] = n.get("total", 0) + 1
        n["last_used"] = now
        proxy_url = n.get("proxy_url") or f"http://{n.get('ip', '127.0.0.1')}:{NODE_PORT}"
        return {"id": f"node:{nid}", "nid": nid, "proxy_url": proxy_url,
                "label": f"node-{nid}", "source": "node"}

    def get_any_key(self, prefer_node=False):
        """Try local pool first, fall back to routing through an online node."""
        with self._lock:
            now = time.time()
            if prefer_node:
                route = self._pick_node_route(now)
                if route:
                    return route
            k = self._pick_local_key(now)
            if k:
                return k
            return self._pick_node_route(now)

    def pick_node(self):
        """Pick an online node (hub assigns keys via /next-key when node runs the request)."""
        with self._lock:
            return self._pick_node_route(time.time())

    def peek_key(self):
        """Check availability without incrementing total or setting last_used."""
        with self._lock:
            now = time.time()
            active = [k for k, v in self.keys.items() if v["active"] and v["cool_until"] < now]
            if not active:
                return None
            kid = active[0]
            k = self.keys[kid]
            return {"id": kid, "key": k["key"], "label": k["label"], "source": "local"}

    def report_error(self, kid, code=429):
        with self._lock:
            k = self.keys.get(kid)
            if not k:
                return
            k["errors"] += 1
            if code == 429:
                backoff = min(30 * (2 ** min(k["errors"] - 1, 2)), 120)
            elif code in (403, 503):
                backoff = min(30 * k["errors"], 90)
            else:
                backoff = min(300 * (2 ** min(k["errors"] - 1, 4)), 3600)
            k["cool_until"] = time.time() + backoff
            if k["errors"] >= 15:
                k["active"] = False
            self._save()

    def report_ok(self, kid):
        with self._lock:
            k = self.keys.get(kid)
            if k:
                k["errors"] = max(0, k["errors"] - 1)
                now = time.time()
                elapsed = now - k.get("last_used", now)
                # Decrement remaining cooldown by time elapsed since last use
                remaining = max(0, k["cool_until"] - now - elapsed)
                if remaining <= 0 and k["errors"] == 0:
                    k["cool_until"] = 0
                else:
                    k["cool_until"] = now + remaining
                self._save()

    def report_tokens(self, kid, prompt_tokens, completion_tokens):
        with self._lock:
            k = self.keys.get(kid)
            if k:
                k["tokens_input"] += prompt_tokens
                k["tokens_output"] += completion_tokens
                self._save()

    def list_keys(self):
        with self._lock:
            now = time.time()
            return {kid: {k: v for k, v in kv.items() if k != "key"} | {
                "cool": kv["cool_until"] > now,
                "cool_remaining": max(0, int(kv["cool_until"] - now))
            } for kid, kv in self.keys.items()}

    def register_node(self, name, ip, device="unknown", key=None, proxy_url=None, node_id=None):
        with self._lock:
            if node_id and node_id in self.nodes:
                nid = node_id
                n = self.nodes[nid]
                n.update({"name": name, "ip": ip, "device": device, "seen": time.time(),
                          "proxy_url": proxy_url or n.get("proxy_url") or f"http://{ip}:{NODE_PORT}"})
                if key:
                    n["key"] = key
            elif node_id:
                # Re-adopt a pruned node id (same device reconnecting)
                nid = node_id
                self.nodes[nid] = {
                    "name": name, "ip": ip, "device": device, "seen": time.time(),
                    "proxy_url": proxy_url or f"http://{ip}:{NODE_PORT}",
                }
                if key:
                    self.nodes[nid]["key"] = key
            else:
                nid = str(uuid.uuid4())[:8]
                self.nodes[nid] = {
                    "name": name, "ip": ip, "device": device, "seen": time.time(),
                    "proxy_url": proxy_url or f"http://{ip}:{NODE_PORT}",
                }
                if key:
                    self.nodes[nid]["key"] = key
            self._save()
        return nid

    def active_node_count(self):
        with self._lock:
            return len(self._online_nodes())

    def heartbeat(self, nid, in_flight=0, uptime=0, tokens_proxied=0, public_ip=""):
        with self._lock:
            if nid in self.nodes:
                n = self.nodes[nid]
                n["seen"] = time.time()
                n["in_flight"] = in_flight
                n["uptime"] = uptime
                n["tokens_proxied"] = tokens_proxied
                if public_ip:
                    n["public_ip"] = public_ip
                self._save()

    def list_nodes(self):
        with self._lock:
            now = time.time()
            return {nid: {"name": n.get("name", "?"), "ip": n.get("ip", "?"),
                          "device": n.get("device", "?"),
                          "online": now - n.get("seen", 0) < 60,
                          "public_ip": n.get("public_ip", ""),
                          "in_flight": n.get("in_flight", 0),
                          "uptime": n.get("uptime", 0),
                          "tokens_proxied": n.get("tokens_proxied", 0)}
                    for nid, n in self.nodes.items()}

    def remove_node(self, nid):
        with self._lock:
            if nid in self.nodes:
                del self.nodes[nid]
                self._save()
                return True
        return False

    def prune(self, timeout=90):
        with self._lock:
            now = time.time()
            dead = [nid for nid, n in self.nodes.items() if now - n.get("seen", 0) > timeout]
            for nid in dead:
                del self.nodes[nid]
            if dead:
                self._save()
            return dead


class WorkQueue:
    """Assign chat requests to nodes that poll the hub (works through NAT)."""

    def __init__(self):
        self._pending = {}
        self._lock = threading.Lock()

    def dispatch(self, body, nid, timeout=WORK_TIMEOUT):
        req_id = str(uuid.uuid4())[:12]
        evt = threading.Event()
        with self._lock:
            self._pending[req_id] = {
                "body": body, "nid": nid, "event": evt,
                "status": 504, "result": b"", "headers": {}, "claimed": False,
            }
        if not evt.wait(timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            return None
        with self._lock:
            item = self._pending.pop(req_id, None)
        return item

    def poll(self, nid):
        with self._lock:
            for req_id, item in self._pending.items():
                if item["nid"] == nid and not item["claimed"]:
                    item["claimed"] = True
                    return {"request_id": req_id, "body": item["body"]}
        return None

    def complete(self, req_id, status, result, headers=None):
        with self._lock:
            item = self._pending.get(req_id)
            if not item:
                return False
            item["status"] = status
            item["result"] = result if isinstance(result, bytes) else str(result).encode()
            item["headers"] = headers or {}
            item["event"].set()
        return True

    def cancel_for_node(self, nid):
        with self._lock:
            dead = [rid for rid, item in self._pending.items() if item["nid"] == nid]
            for rid in dead:
                item = self._pending[rid]
                if not item["event"].is_set():
                    item["status"] = 503
                    item["result"] = json.dumps({"error": "node offline"}).encode()
                    item["headers"] = {"Content-Type": "application/json"}
                    item["event"].set()


# ─── Hub HTTP Server ─────────────────────────────────────────────────

def run_hub():
    pool = KeyPool()
    work_queue = WorkQueue()
    conn_cache = ConnectionCache()
    global _work_queue_for_shutdown
    _work_queue_for_shutdown = work_queue

    class HubHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _s(self, data, code=200):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def _e(self, msg, code=400):
            self._s({"error": msg}, code)

        def _auth(self):
            if not AUTH_SECRET:
                return True
            header = self.headers.get("Authorization", "")
            if header == f"Bearer {AUTH_SECRET}":
                return True
            self._e("unauthorized", 401)
            return False

        def _body(self):
            n_str = self.headers.get("Content-Length", "0")
            try:
                n = int(n_str)
            except (ValueError, TypeError):
                n = 0
            if n > MAX_BODY:
                self._e("request body too large", 413)
                return None
            n = min(n, MAX_BODY)
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
                now = time.time()
                online = sum(1 for n in pool.nodes.values() if now - n.get("seen", 0) < 120)
                total_in_flight = sum(n.get("in_flight", 0) for n in pool.nodes.values())
                total_tokens = sum(n.get("tokens_proxied", 0) for n in pool.nodes.values())
                self._s({"ok": True, "host": platform.node(),
                         "keys": len(pool.keys), "online_nodes": online,
                         "nodes": len(pool.nodes),
                         "total_in_flight": total_in_flight,
                         "total_tokens_proxied": total_tokens})
            elif p == "/v1/models":
                self._s({"object": "list", "data": [
                    {"id": "big-pickle", "object": "model", "owned_by": "opencode"},
                    {"id": "deepseek-v4-flash-free", "object": "model", "owned_by": "opencode"},
                    {"id": "deepseek-v4-pro", "object": "model", "owned_by": "opencode"},
                    {"id": "nemotron-3-super-free", "object": "model", "owned_by": "opencode"},
                    {"id": "mimo-v2.5-free", "object": "model", "owned_by": "opencode"},
                ]})
            elif p == "/keys":
                self._s({"keys": pool.list_keys()})
            elif p == "/nodes":
                self._s({"nodes": pool.list_nodes()})
            elif p == "/metrics":
                self._metrics()
            elif p == "/status":
                self._status()
            else:
                self._e("not found", 404)

        def do_POST(self):
            if not self._auth():
                return
            p = self.path.split("?")[0]
            try:
                b = self._body()
                if b is None:
                    return
            except Exception:
                return self._e("bad json")
            if p == "/keys":
                if not b.get("key"):
                    return self._e("missing key")
                kid = pool.add_key(b["key"], b.get("label", ""))
                self._s({"id": kid})
            elif p == "/register":
                nid = pool.register_node(
                    b.get("name", f"n-{len(pool.nodes)+1}"),
                    self.client_address[0],
                    b.get("device"),
                    key=b.get("key"),
                    proxy_url=b.get("proxy_url"),
                    node_id=b.get("node_id"),
                )
                self._s({"node_id": nid, "interval": HEARTBEAT_INTERVAL})
            elif p == "/heartbeat":
                pool.heartbeat(
                    b.get("node_id"),
                    in_flight=b.get("in_flight", 0),
                    uptime=b.get("uptime", 0),
                    tokens_proxied=b.get("tokens_proxied", 0),
                    public_ip=b.get("public_ip", ""),
                )
                self._s({"ok": True})
            elif p == "/poll-work":
                work = work_queue.poll(b.get("node_id"))
                self._s(work or {"work": None})
            elif p == "/complete-work":
                raw = b.get("body", "")
                if isinstance(raw, str):
                    raw = raw.encode()
                elif not isinstance(raw, bytes):
                    raw = b""
                ok = work_queue.complete(
                    b.get("request_id"), b.get("status", 502), raw, b.get("headers"))
                self._s({"ok": ok})
            elif p == "/next-key":
                nid = b.get("node_id")
                if not nid or not pool.node_online(nid):
                    return self._e("unknown or offline node", 403)
                k = pool.get_key_for_node()
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
            elif p.startswith("/keys/") and p.endswith("/reactivate"):
                kid = p.split("/")[2]
                with pool._lock:
                    k = pool.keys.get(kid)
                    if not k:
                        return self._e("key not found", 404)
                    k["active"] = True
                    k["errors"] = 0
                    k["cool_until"] = 0
                    pool._save()
                self._s({"ok": True})
            elif p == "/v1/chat/completions":
                self._proxy(b, pool)
            else:
                self._e("not found", 404)

        def do_DELETE(self):
            if not self._auth():
                return
            p = self.path.split("?")[0]
            if p.startswith("/keys/"):
                pool.remove_key(p.split("/")[-1])
                self._s({"ok": True})
            elif p.startswith("/nodes/"):
                ok = pool.remove_node(p.split("/")[-1])
                self._s({"ok": ok}, 200 if ok else 404)
            else:
                self._e("not found", 404)

        def _metrics(self):
            """Return Prometheus-format metrics."""
            now = time.time()
            lines = []
            lines.append("# HELP zenpool_keys_total Number of keys by status")
            lines.append("# TYPE zenpool_keys_total gauge")
            active = cooled = dead = 0
            for kv in pool.keys.values():
                if not kv.get("active", True):
                    dead += 1
                elif kv["cool_until"] > now:
                    cooled += 1
                else:
                    active += 1
            lines.append(f'zenpool_keys_total{{status="active"}} {active}')
            lines.append(f'zenpool_keys_total{{status="cooled"}} {cooled}')
            lines.append(f'zenpool_keys_total{{status="dead"}} {dead}')

            lines.append("# HELP zenpool_key_errors Total errors per key")
            lines.append("# TYPE zenpool_key_errors counter")
            for kid, kv in pool.keys.items():
                lines.append(f'zenpool_key_errors{{key_id="{kid}",label="{kv.get("label","")}"}} {kv["errors"]}')

            lines.append("# HELP zenpool_key_tokens_input Total input tokens per key")
            lines.append("# TYPE zenpool_key_tokens_input counter")
            for kid, kv in pool.keys.items():
                lines.append(f'zenpool_key_tokens_input{{key_id="{kid}",label="{kv.get("label","")}"}} {kv["tokens_input"]}')

            lines.append("# HELP zenpool_key_tokens_output Total output tokens per key")
            lines.append("# TYPE zenpool_key_tokens_output counter")
            for kid, kv in pool.keys.items():
                lines.append(f'zenpool_key_tokens_output{{key_id="{kid}",label="{kv.get("label","")}"}} {kv["tokens_output"]}')

            lines.append("# HELP zenpool_key_total Total requests per key")
            lines.append("# TYPE zenpool_key_total counter")
            for kid, kv in pool.keys.items():
                lines.append(f'zenpool_key_total{{key_id="{kid}",label="{kv.get("label","")}"}} {kv["total"]}')

            lines.append("# HELP zenpool_nodes_online Nodes currently online")
            lines.append("# TYPE zenpool_nodes_online gauge")
            lines.append(f"zenpool_nodes_online {pool.active_node_count()}")

            lines.append("# HELP zenpool_nodes_total Total registered nodes")
            lines.append("# TYPE zenpool_nodes_total gauge")
            lines.append(f"zenpool_nodes_total {len(pool.nodes)}")

            lines.append("# HELP zenpool_uptime_seconds Hub uptime")
            lines.append("# TYPE zenpool_uptime_seconds gauge")
            lines.append(f"zenpool_uptime_seconds {int(time.time() - pool._start)}")

            body = "\n".join(lines) + "\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body.encode())

        def _status(self):
            """Return rich JSON status."""
            with pool._lock:
                now = time.time()
                active_keys = sum(1 for v in pool.keys.values() if v["active"] and v["cool_until"] < now)
                cooled_keys = sum(1 for v in pool.keys.values() if v["cool_until"] > now)
                dead_keys = sum(1 for v in pool.keys.values() if not v["active"])

                per_key = {}
                for kid, kv in pool.keys.items():
                    per_key[kid] = {
                        "label": kv["label"],
                        "active": kv["active"],
                        "cool": kv["cool_until"] > now,
                        "cool_remaining": max(0, int(kv["cool_until"] - now)),
                        "total": kv["total"],
                        "errors": kv["errors"],
                        "tokens_input": kv["tokens_input"],
                        "tokens_output": kv["tokens_output"],
                    }

                now_n = time.time()
                online_nodes = sum(1 for n in pool.nodes.values() if now_n - n.get("seen", 0) < 60)
                per_node = {}
                for nid, n in pool.nodes.items():
                    per_node[nid] = {
                        "name": n.get("name", "?"),
                        "device": n.get("device", "?"),
                        "online": now_n - n.get("seen", 0) < 60,
                        "total": n.get("total", 0),
                    }

                total_requests = sum(v["total"] for v in pool.keys.values())
                total_tokens = sum(v["tokens_input"] + v["tokens_output"] for v in pool.keys.values())

            self._s({
                "version": VERSION,
                "uptime": int(time.time() - pool._start),
                "total_requests": total_requests,
                "total_tokens": total_tokens,
                "active_keys": active_keys,
                "cooled_keys": cooled_keys,
                "dead_keys": dead_keys,
                "online_nodes": online_nodes,
                "total_nodes": len(pool.nodes),
                "keys": per_key,
                "nodes": per_node,
            })

        def _relay_response(self, r):
            ctype = r.headers.get("Content-Type", "application/json")
            self.send_response(r.status)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if "text/event-stream" in ctype:
                while True:
                    chunk = r.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            else:
                self.wfile.write(r.read())

        def _forward_to_node(self, body, k, timeout=PUSH_TIMEOUT):
            """Route request through the node proxy so OpenCode sees the node's IP."""
            url = f"{k['proxy_url'].rstrip('/')}/v1/chat/completions"
            parsed = urllib.parse.urlparse(url)
            host = parsed.hostname or "localhost"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            opener = conn_cache.get_opener(host, port)
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "X-ZenPool-Hub": "1",
                         "User-Agent": "curl/7.76.1"},
            )
            with opener.open(req, timeout=timeout) as r:
                self._relay_response(r)

        def _dispatch_via_node(self, body, k):
            """Pull work via hub queue (NAT-safe), then optional quick push."""
            item = work_queue.dispatch(body, k["nid"], timeout=PULL_TIMEOUT)
            if item:
                self.send_response(item["status"])
                for hk, hv in item.get("headers", {}).items():
                    if hk.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(hk, hv)
                if not item.get("headers"):
                    self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(item["result"])
                return True
            try:
                self._forward_to_node(body, k)
                return True
            except urllib.error.HTTPError as e:
                raw = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(raw)
                return True
            except (urllib.error.URLError, OSError, TimeoutError):
                return False

        def _proxy(self, body, pool):
            tried_local = set()
            node_attempts = 0
            max_node_attempts = max(pool.active_node_count() * 5, 5)
            max_tries = max(len(pool.keys) + pool.active_node_count(), 1) * 3
            last_resp = None
            last_code = 503
            prefer_node = pool.active_node_count() > 0 and not pool.peek_key()

            for _ in range(max_tries):
                k = pool.get_any_key(prefer_node=prefer_node)
                prefer_node = False
                if not k:
                    k = pool.get_key_for_node()
                    if k:
                        k = {**k, "source": "local-fallback"}
                if not k:
                    break

                if k.get("source") == "node":
                    node_attempts += 1
                    if node_attempts > max_node_attempts:
                        prefer_node = False
                        continue
                    if self._dispatch_via_node(body, k):
                        return
                    prefer_node = True
                    continue

                kid = k["id"]
                if kid in tried_local:
                    prefer_node = pool.active_node_count() > 0
                    continue
                tried_local.add(kid)

                req = urllib.request.Request(
                    ZEN_API,
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {k['key']}",
                             "User-Agent": "curl/7.76.1"}
                )
                try:
                    parsed_zen = urllib.parse.urlparse(ZEN_API)
                    zhost = parsed_zen.hostname or "opencode.ai"
                    zport = parsed_zen.port or (443 if parsed_zen.scheme == "https" else 80)
                    zen_opener = conn_cache.get_opener(zhost, zport)
                    with zen_opener.open(req, timeout=120) as r:
                        body = r.read()
                        # Track tokens from JSON response body
                        try:
                            jbody = json.loads(body)
                            usage = jbody.get("usage", {}) or {}
                            pt = usage.get("prompt_tokens")
                            ct = usage.get("completion_tokens")
                            if pt is not None and ct is not None:
                                pool.report_tokens(kid, pt, ct)
                        except (json.JSONDecodeError, AttributeError, TypeError):
                            pass
                        pool.report_ok(kid)
                        ctype = r.headers.get("Content-Type", "application/json")
                        self.send_response(r.status)
                        self.send_header("Content-Type", ctype)
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        self.wfile.write(body)
                        return
                except urllib.error.HTTPError as e:
                    raw = b""
                    try:
                        raw = e.read()
                        resp_body = json.loads(raw) if raw else {"error": f"HTTP {e.code}"}
                    except (json.JSONDecodeError, TypeError):
                        resp_body = {"error": f"upstream HTTP {e.code}", "detail": raw.decode(errors='replace')[:200]}
                    last_resp = resp_body
                    last_code = e.code
                    if e.code == 429:
                        pool.report_error(kid, 429)
                        prefer_node = pool.active_node_count() > 0
                        continue
                    if e.code == 503:
                        prefer_node = pool.active_node_count() > 0
                        continue
                    if e.code == 403:
                        pool.report_error(kid, 403)
                        prefer_node = pool.active_node_count() > 0
                        continue
                    if e.code >= 500:
                        prefer_node = pool.active_node_count() > 0
                        continue
                    self._s(resp_body, e.code)
                    return
                except urllib.error.URLError as e:
                    return self._e(f"upstream unreachable: {e.reason}", 502)
                except Exception as e:
                    return self._e(f"proxy error: {e}", 502)

            if last_resp is not None:
                return self._s(last_resp, last_code)
            return self._e("no keys available", 503)

    def _prune():
        while True:
            time.sleep(15)
            dead = pool.prune()
            for nid in dead:
                work_queue.cancel_for_node(nid)

    threading.Thread(target=_prune, daemon=True).start()
    print(f"\n  🐍 ZenPool Hub v{VERSION}  —  {platform.node()}")
    print(f"  ├─ Port: {HUB_PORT}")
    print(f"  ├─ Keys: {len(pool.keys)}")
    print(f"  └─ Data: {os.path.abspath(DATA_FILE)}")
    print("\n  Endpoints:")
    print("    GET  /health   GET /status   GET /metrics               (observability)")
    print("    GET  /keys     POST /keys    DELETE /keys/<id>")
    print("    POST /register  POST /heartbeat  POST /next-key  POST /report")
    print("    POST /keys/<id>/reactivate")
    print("    POST /v1/chat/completions  (direct proxy)")
    print(f"\n  Deploy nodes: curl -fsSL <url> | python3 - node --hub http://<this-ip>:{HUB_PORT}")
    print()
    ThreadingHTTPServer(("0.0.0.0", HUB_PORT), HubHandler).serve_forever()


# ═══════════════════════════════════════════════════════════════════════
#  NODE
# ═══════════════════════════════════════════════════════════════════════

class NodeClient:
    def __init__(self, hub, local_key=None, proxy_url=None, state_dir=None,
                 max_workers=None):
        self.hub = hub.rstrip("/")
        self.local_key = local_key
        self.proxy_url = proxy_url or os.environ.get("ZENPOOL_PUBLIC_URL")
        self.max_workers = max_workers or int(os.environ.get("ZENPOOL_NODE_MAX_WORKERS", "8"))
        self._work_semaphore = threading.BoundedSemaphore(self.max_workers)
        self.state_dir = state_dir or os.environ.get(
            "ZENPOOL_STATE", _default_state_dir())
        self.state_file = os.path.join(self.state_dir, "node-state.json")
        self.nid = self._load_nid()
        self.hub_ok = False
        self.name = platform.node() or "unknown"
        self.device = f"{platform.system()}/{platform.machine()}"
        # Connection cache for keep-alive connection reuse
        self._conn_cache = ConnectionCache()
        # Heartbeat enrichment
        self.start_time = time.time()
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()
        self.tokens_proxied = 0
        self.public_ip = "unknown"

    def _load_nid(self):
        try:
            with open(self.state_file) as f:
                return json.load(f).get("node_id")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _save_nid(self):
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump({"node_id": self.nid, "hub": self.hub}, f)
        except OSError:
            pass

    def _call(self, path, data=None):
        body = json.dumps(data).encode() if data else None
        try:
            parsed = urllib.parse.urlparse(self.hub)
            host = parsed.hostname or "localhost"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            opener = self._conn_cache.get_opener(host, port)
            r = opener.open(
                urllib.request.Request(f"{self.hub}{path}", data=body,
                                       headers={"Content-Type": "application/json"}),
                timeout=10
            )
            return json.loads(r.read())
        except Exception as e:
            return {"error": str(e)}

    def register(self):
        payload = {"name": self.name, "device": self.device}
        if self.nid:
            payload["node_id"] = self.nid
        if self.local_key:
            payload["key"] = self.local_key
        if self.proxy_url:
            payload["proxy_url"] = self.proxy_url.rstrip("/")
        r = self._call("/register", payload)
        if r.get("node_id"):
            self.nid = r["node_id"]
            self.hub_ok = True
            self._save_nid()
            return True
        self.hub_ok = False
        return False

    def heartbeat(self):
        with self._in_flight_lock:
            in_flight = self._in_flight
        uptime = int(time.time() - self.start_time)
        payload = {
            "node_id": self.nid,
            "in_flight": in_flight,
            "uptime": uptime,
            "tokens_proxied": self.tokens_proxied,
            "public_ip": self.public_ip,
        }
        r = self._call("/heartbeat", payload)
        self.hub_ok = bool(r.get("ok")) and not r.get("error")

    def poll_work(self):
        return self._call("/poll-work", {"node_id": self.nid})

    def complete_work(self, request_id, status, body, headers=None):
        payload = {
            "request_id": request_id,
            "status": status,
            "body": body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body,
            "headers": headers or {},
        }
        self._call("/complete-work", payload)

    def run_hub_work(self, work):
        """Execute a hub-assigned request from this node's IP using a hub pool key."""
        with self._in_flight_lock:
            self._in_flight += 1
        try:
            body = work.get("body")
            req_id = work.get("request_id")
            if not body or not req_id:
                return
            key = {"key": self.local_key, "id": None} if self.local_key else self.next_key()
            if not key or not key.get("key"):
                self.complete_work(req_id, 503, json.dumps({"error": "no keys available"}),
                                   {"Content-Type": "application/json"})
                return
            headers = {"Content-Type": "application/json", "User-Agent": "curl/7.76.1",
                       "Authorization": f"Bearer {key['key']}"}
            req = urllib.request.Request(ZEN_API, data=json.dumps(body).encode(), headers=headers)
            try:
                parsed = urllib.parse.urlparse(ZEN_API)
                zhost = parsed.hostname or "opencode.ai"
                zport = parsed.port or (443 if parsed.scheme == "https" else 80)
                zen_opener = self._conn_cache.get_opener(zhost, zport)
                with zen_opener.open(req, timeout=WORK_TIMEOUT) as r:
                    data = r.read()
                    # Track tokens from response
                    try:
                        jbody = json.loads(data)
                        usage = jbody.get("usage", {}) or {}
                        tt = usage.get("total_tokens", 0) or 0
                        self.tokens_proxied += tt
                    except (json.JSONDecodeError, AttributeError, TypeError):
                        pass
                    if key.get("id"):
                        self.report(key["id"], ok=True)
                    self.complete_work(req_id, r.status, data,
                                       {"Content-Type": r.headers.get("Content-Type", "application/json")})
            except urllib.error.HTTPError as e:
                if key.get("id"):
                    self.report(key["id"], ok=False)
                self.complete_work(req_id, e.code, e.read(), {"Content-Type": "application/json"})
            except Exception as e:
                self.complete_work(req_id, 502, json.dumps({"error": str(e)}),
                                   {"Content-Type": "application/json"})
        finally:
            with self._in_flight_lock:
                self._in_flight -= 1
            self._work_semaphore.release()

    def next_key(self):
        r = self._call("/next-key", {"node_id": self.nid})
        return r if r.get("key") else None

    def report(self, kid, ok=True):
        self._call("/report", {"key_id": kid, "ok": ok, "node_id": self.nid})


def run_node(hub_url, local_key=None, proxy_url=None):
    client = NodeClient(hub_url, local_key=local_key, proxy_url=proxy_url)

    class NodeHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
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
                self._s({"ok": True, "node": client.nid, "hub": client.hub,
                         "registered": client.hub_ok,
                         "public_ip": client.public_ip,
                         "uptime": int(time.time() - client.start_time),
                         "in_flight": client._in_flight,
                         "tokens_proxied": client.tokens_proxied})
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
            parsed = urllib.parse.urlparse(ZEN_API)
            zhost = parsed.hostname or "opencode.ai"
            zport = parsed.port or (443 if parsed.scheme == "https" else 80)
            zen_opener = client._conn_cache.get_opener(zhost, zport)
            req = urllib.request.Request(
                ZEN_API, data=json.dumps(body).encode(), headers=headers
            )
            with client._in_flight_lock:
                client._in_flight += 1
            try:
                with zen_opener.open(req, timeout=180) as r:
                    data = r.read()
                    # Track tokens from response
                    try:
                        jbody = json.loads(data)
                        usage = jbody.get("usage", {}) or {}
                        tt = usage.get("total_tokens", 0) or 0
                        client.tokens_proxied += tt
                    except (json.JSONDecodeError, AttributeError, TypeError):
                        pass
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
                raw = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(raw)
            except Exception as e:
                self._e(str(e), 502)
            finally:
                with client._in_flight_lock:
                    client._in_flight -= 1

    # Register + heartbeat + poll hub for work (NAT-safe)
    def _loop():
        while True:
            if not client.nid or not client.hub_ok:
                if client.register():
                    print(f"  ✅ Connected to hub: {client.nid}")
                elif client.nid:
                    print(f"  ⚠️  Hub unreachable — retrying (cached id: {client.nid})")
            else:
                client.heartbeat()
            time.sleep(HEARTBEAT_INTERVAL)

    def _poll_loop():
        while True:
            if client.nid:
                work = client.poll_work()
                if work and work.get("request_id"):
                    # Throttle concurrent work dispatch with bounded semaphore
                    client._work_semaphore.acquire()
                    threading.Thread(target=client.run_hub_work, args=(work,), daemon=True).start()
            time.sleep(POLL_INTERVAL)

    print(f"\n  🐍 ZenPool Node v{VERSION}  —  {client.name}")
    print(f"  ├─ Hub: {client.hub}")
    print(f"  ├─ Port: {NODE_PORT}")
    print(f"  ├─ Workers: {client.max_workers}")
    print(f"  ├─ Device: {client.device}")
    print(f"  ├─ Public IP: {client.public_ip}")
    print(f"  └─ State: {client.state_dir}")
    print()
    client.register()

    def _ip_loop():
        """Every 15s fetch public IP from AWS checkip, store in state file."""
        while True:
            try:
                ip_req = urllib.request.Request("http://checkip.amazonaws.com")
                with urllib.request.urlopen(ip_req, timeout=5) as r:
                    client.public_ip = r.read().decode().strip()
                # Persist to state file
                try:
                    with open(client.state_file) as sf:
                        state = json.load(sf)
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    state = {}
                state["public_ip"] = client.public_ip
                try:
                    with open(client.state_file, "w") as sf:
                        json.dump(state, sf)
                except OSError:
                    pass
            except Exception:
                client.public_ip = "unknown"
            time.sleep(15)

    threading.Thread(target=_loop, daemon=True).start()
    threading.Thread(target=_poll_loop, daemon=True).start()
    threading.Thread(target=_ip_loop, daemon=True).start()

    print(f"  🚀 Hub endpoint: {client.hub}/v1/chat/completions")
    print(f"  🔄 Auto-registers; pulls keys from hub when running requests")
    print(f"  📡 Local proxy: http://localhost:{NODE_PORT}/v1/chat/completions")
    print(f"  🪟 Windows: (Invoke-WebRequest -Uri <url>/zenpool.py).Content | python - node")
    print()
    try:
        ThreadingHTTPServer(("0.0.0.0", NODE_PORT), NodeHandler).serve_forever()
    except OSError as e:
        if e.errno == 98 or "Address already in use" in str(e):
            print(f"  ❌ Port {NODE_PORT} already in use — another zenpool node is running.")
            print(f"     Stop it:  pkill -u \"$(id -u)\" -f 'zenpool.py.*node'")
            print(f"     Or check: curl -s http://localhost:{NODE_PORT}/health")
            raise SystemExit(1) from e
        raise


# ═══════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(prog="zenpool", description="ZenPool — distributed key proxy for OpenCode\n"
        "  Linux/Mac:  curl -fsSL <url>/zenpool.py | python3 - node\n"
        "  PowerShell: (Invoke-WebRequest -Uri <url>/zenpool.py).Content | python - node")
    p.add_argument("mode", choices=["hub", "node"], help="run as hub (server) or node (agent)")
    p.add_argument("--hub", default=DEFAULT_HUB, help="hub URL (for node mode, default: " + DEFAULT_HUB + ")")
    p.add_argument("--key", default=None, help="optional local key override (default: keys from hub pool)")
    p.add_argument("--public-url", default=None,
                   help="reachable URL for this node proxy (e.g. http://100.x.x.x:5052 for Tailscale)")

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
        run_node(args.hub, local_key=args.key, proxy_url=args.public_url)
