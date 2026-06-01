#!/usr/bin/env python3
"""zenpool — Unified API key pool & distributed proxy for OpenCode Zen.

One file, two modes:
  python3 zenpool.py hub         → Run the central hub (on your main server)
  python3 zenpool.py node        → Run a node agent (on any device)
  python3 zenpool.py node --hub http://x:5051  → Connect to specific hub

Install: curl -fsSL https://srv880434.hstgr.cloud/zenpool.py | python3 - node
Windows: (Invoke-WebRequest -Uri https://srv880434.hstgr.cloud/zenpool.py).Content | python - node
"""
import hashlib
import ipaddress
import json
import os
import platform
import signal
import sqlite3
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
import urllib.parse
from contextlib import contextmanager
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ─── Config ──────────────────────────────────────────────────────────

VERSION = "3.1.0"
DEFAULT_HUB = os.environ.get("ZENPOOL_HUB", "https://srv880434.hstgr.cloud")
HUB_PORT = int(os.environ.get("ZENPOOL_PORT", 5051))
NODE_PORT = int(os.environ.get("ZENPOOL_NODE_PORT", 5052))
DATA_FILE = os.environ.get("ZENPOOL_DATA", "zenpool.db")
HEARTBEAT_INTERVAL = 30
POLL_INTERVAL = 1
WORK_TIMEOUT = 120
PUSH_TIMEOUT = 4
PULL_TIMEOUT = 90
MAX_BODY = int(os.environ.get("ZENPOOL_MAX_BODY", 10 * 1024 * 1024))
AUTH_SECRET = os.environ.get("ZENPOOL_SECRET", "")
REQUIRE_AUTH = os.environ.get("ZENPOOL_REQUIRE_AUTH", "1") == "1"
ZEN_API = "https://opencode.ai/zen/v1/chat/completions"

BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


# ─── Cross-platform state directory ──────────────────────────────────

def _default_state_dir() -> str:
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA",
                              os.path.join(os.path.expanduser("~"), "AppData", "Local"))
        return os.path.join(base, "zenpool")
    if system == "Darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "zenpool")
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return os.path.join(xdg, "zenpool")
    return os.path.join(os.path.expanduser("~"), ".local", "share", "zenpool")


# ─── Graceful shutdown ───────────────────────────────────────────────

shutting_down = False
_work_queue_for_shutdown = None


def _handle_signal(signum, frame):
    global shutting_down
    if shutting_down:
        sys.exit(1)
    shutting_down = True
    name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
    print(f"\n  ␡ {name} received — shutting down gracefully...")
    wq = _work_queue_for_shutdown
    if wq is not None:
        with wq._lock:
            for item in wq._pending.values():
                item["event"].set()
            wq._pending.clear()
        print("  ✓ Cancelled pending work items")
    sys.exit(0)


try:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
except (AttributeError, ValueError, OSError):
    pass


# ─── Security utilities ──────────────────────────────────────────────

def validate_proxy_url(url: str) -> tuple[bool, str]:
    """Block SSRF targets in node proxy URLs."""
    if not url:
        return True, ""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, f"invalid scheme: {parsed.scheme}"
        host = parsed.hostname
        if not host:
            return False, "missing hostname"
        try:
            ip = ipaddress.ip_address(host)
            for net in BLOCKED_NETWORKS:
                if ip in net:
                    return False, f"blocked network: {net}"
        except ValueError:
            if host in ("localhost", "localhost.localdomain"):
                return False, "localhost blocked"
            if host.endswith((".local", ".internal")):
                return False, "internal domain blocked"
        return True, ""
    except Exception as e:
        return False, str(e)


def mask_key(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


def generate_node_token(node_id: str, secret: str) -> str:
    return hashlib.sha256(f"{node_id}:{secret}".encode()).hexdigest()[:32]


def verify_node_token(node_id: str, token: str, secret: str) -> bool:
    return token == generate_node_token(node_id, secret)


# ─── RWLock ──────────────────────────────────────────────────────────

class RWLock:
    """Multiple readers OR single writer."""

    def __init__(self):
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writers_waiting = 0
        self._writer_active = False

    @contextmanager
    def read(self):
        with self._cond:
            while self._writer_active or self._writers_waiting > 0:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextmanager
    def write(self):
        with self._cond:
            self._writers_waiting += 1
            while self._readers > 0 or self._writer_active:
                self._cond.wait()
            self._writers_waiting -= 1
            self._writer_active = True
        try:
            yield
        finally:
            with self._cond:
                self._writer_active = False
                self._cond.notify_all()


# ─── SQLite Storage ──────────────────────────────────────────────────

class Storage:
    """SQLite WAL-mode persistence. Thread-local connections for safety."""

    def __init__(self, path: str = DATA_FILE):
        self.path = path
        self._local = threading.local()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        c = self._conn()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS keys (
                id TEXT PRIMARY KEY,
                key TEXT NOT NULL,
                label TEXT DEFAULT '',
                active INTEGER DEFAULT 1,
                cool_until REAL DEFAULT 0,
                total INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0,
                tokens_input INTEGER DEFAULT 0,
                tokens_output INTEGER DEFAULT 0,
                last_used REAL DEFAULT 0,
                created_at REAL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                name TEXT DEFAULT '',
                ip TEXT DEFAULT '',
                device TEXT DEFAULT '',
                proxy_url TEXT DEFAULT '',
                token TEXT DEFAULT '',
                key TEXT DEFAULT '',
                seen REAL DEFAULT 0,
                total INTEGER DEFAULT 0,
                in_flight INTEGER DEFAULT 0,
                uptime INTEGER DEFAULT 0,
                tokens_proxied INTEGER DEFAULT 0,
                public_ip TEXT DEFAULT '',
                created_at REAL DEFAULT (strftime('%s','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_keys_cool ON keys(active, cool_until);
            CREATE INDEX IF NOT EXISTS idx_nodes_seen ON nodes(seen);
        """)
        c.commit()

    def migrate_json(self, json_path: str):
        """One-time import from zenpool-data.json."""
        if not os.path.exists(json_path):
            return
        try:
            with open(json_path) as f:
                d = json.load(f)
            c = self._conn()
            for kid, kv in d.get("keys", {}).items():
                c.execute(
                    "INSERT OR IGNORE INTO keys "
                    "(id, key, label, active, cool_until, total, errors, tokens_input, tokens_output) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (kid, kv.get("key", ""), kv.get("label", kid[:8]),
                     1 if kv.get("active", True) else 0,
                     kv.get("cool_until", 0), kv.get("total", 0), kv.get("errors", 0),
                     kv.get("tokens_input", 0), kv.get("tokens_output", 0)),
                )
            for nid, nv in d.get("nodes", {}).items():
                c.execute(
                    "INSERT OR IGNORE INTO nodes (id, name, ip, device, proxy_url, key, seen, total) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (nid, nv.get("name", ""), nv.get("ip", ""), nv.get("device", ""),
                     nv.get("proxy_url", ""), nv.get("key", ""),
                     nv.get("seen", 0), nv.get("total", 0)),
                )
            c.commit()
            os.rename(json_path, json_path + ".migrated")
            print(f"  ✓ Migrated {json_path} → SQLite")
        except Exception as e:
            print(f"  ⚠️  Migration failed: {e}", file=sys.stderr)

    # ── Keys ─────────────────────────────────────────────────────────

    def add_key(self, key: str, label: str = "") -> str:
        kid = hashlib.sha256(key.encode()).hexdigest()[:12]
        c = self._conn()
        c.execute("INSERT OR IGNORE INTO keys (id, key, label) VALUES (?, ?, ?)",
                  (kid, key, label or kid[:8]))
        c.commit()
        return kid

    def get_key(self, kid: str) -> dict | None:
        row = self._conn().execute("SELECT * FROM keys WHERE id = ?", (kid,)).fetchone()
        return dict(row) if row else None

    def get_active_keys(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM keys WHERE active = 1 AND cool_until < ?", (time.time(),)).fetchall()
        return [dict(r) for r in rows]

    def get_all_active_keys(self) -> list[dict]:
        rows = self._conn().execute("SELECT * FROM keys WHERE active = 1").fetchall()
        return [dict(r) for r in rows]

    def peek_active_key(self) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM keys WHERE active = 1 AND cool_until < ? LIMIT 1",
            (time.time(),)).fetchone()
        return dict(row) if row else None

    def list_keys(self, include_secret: bool = False) -> dict[str, dict]:
        now = time.time()
        rows = self._conn().execute("SELECT * FROM keys").fetchall()
        result = {}
        for row in rows:
            d = dict(row)
            if not include_secret:
                d["key"] = mask_key(d["key"])
            d["cool"] = d["cool_until"] > now
            d["cool_remaining"] = max(0, int(d["cool_until"] - now))
            result[d["id"]] = d
        return result

    def update_key(self, kid: str, **kwargs):
        if not kwargs:
            return
        c = self._conn()
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        c.execute(f"UPDATE keys SET {sets} WHERE id = ?", (*kwargs.values(), kid))
        c.commit()

    def remove_key(self, kid: str):
        c = self._conn()
        c.execute("DELETE FROM keys WHERE id = ?", (kid,))
        c.commit()

    def key_count(self) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM keys").fetchone()[0]

    # ── Nodes ────────────────────────────────────────────────────────

    def upsert_node(self, nid: str, **kwargs) -> str:
        c = self._conn()
        if c.execute("SELECT id FROM nodes WHERE id = ?", (nid,)).fetchone():
            if kwargs:
                sets = ", ".join(f"{k} = ?" for k in kwargs)
                c.execute(f"UPDATE nodes SET {sets} WHERE id = ?", (*kwargs.values(), nid))
        else:
            kwargs["id"] = nid
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" * len(kwargs))
            c.execute(f"INSERT INTO nodes ({cols}) VALUES ({placeholders})",
                      list(kwargs.values()))
        c.commit()
        return nid

    def get_node(self, nid: str) -> dict | None:
        row = self._conn().execute("SELECT * FROM nodes WHERE id = ?", (nid,)).fetchone()
        return dict(row) if row else None

    def get_online_nodes(self, timeout: int = 120) -> list[dict]:
        cutoff = time.time() - timeout
        rows = self._conn().execute(
            "SELECT * FROM nodes WHERE seen > ?", (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def list_nodes(self) -> dict[str, dict]:
        now = time.time()
        rows = self._conn().execute("SELECT * FROM nodes").fetchall()
        result = {}
        for row in rows:
            d = dict(row)
            d["online"] = now - d.get("seen", 0) < 120
            result[d["id"]] = d
        return result

    def remove_node(self, nid: str):
        c = self._conn()
        c.execute("DELETE FROM nodes WHERE id = ?", (nid,))
        c.commit()

    def prune_nodes(self, timeout: int = 90) -> list[str]:
        cutoff = time.time() - timeout
        rows = self._conn().execute(
            "SELECT id FROM nodes WHERE seen < ?", (cutoff,)).fetchall()
        dead = [r["id"] for r in rows]
        if dead:
            self._conn().execute("DELETE FROM nodes WHERE seen < ?", (cutoff,))
            self._conn().commit()
        return dead

    def node_count(self) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM nodes").fetchone()[0]


# ─── Connection cache ────────────────────────────────────────────────

class ConnectionCache:
    """Keep-alive opener pool keyed by host:port."""

    def __init__(self, max_age: int = 30, max_entries: int = 10):
        self._cache: dict[str, tuple] = {}
        self._max_age = max_age
        self._max_entries = max_entries
        self._lock = threading.Lock()

    def get_opener(self, host: str, port: int):
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

    def _evict(self, now: float):
        stale = [k for k, v in self._cache.items() if (now - v[1]) > self._max_age]
        for k in stale:
            del self._cache[k]
        if len(self._cache) > self._max_entries:
            for k, _ in sorted(self._cache.items(),
                                key=lambda x: x[1][1])[:len(self._cache) - self._max_entries]:
                del self._cache[k]


# ─── KeyPool ─────────────────────────────────────────────────────────

class KeyPool:
    """SQLite-backed key pool with round-robin + exponential backoff."""

    def __init__(self, storage: Storage):
        self.storage = storage
        self._rr = 0
        self._rr_node = 0
        self._rr_lock = threading.Lock()
        self._lock = RWLock()
        self._start = time.time()

    def add_key(self, key: str, label: str = "") -> str:
        return self.storage.add_key(key, label)

    def remove_key(self, kid: str):
        self.storage.remove_key(kid)

    def _pick_from(self, keys: list[dict], source: str) -> dict | None:
        if not keys:
            return None
        with self._rr_lock:
            idx = self._rr % len(keys)
            self._rr = (self._rr + 1) % max(len(keys), 1)
        k = keys[idx]
        self.storage.update_key(k["id"], total=k["total"] + 1, last_used=time.time())
        return {"id": k["id"], "key": k["key"], "label": k["label"], "source": source}

    def get_key(self) -> dict | None:
        with self._lock.read():
            keys = self.storage.get_active_keys()
        return self._pick_from(keys, "local")

    def get_key_for_node(self) -> dict | None:
        with self._lock.read():
            keys = self.storage.get_all_active_keys()
        return self._pick_from(keys, "node-key")

    def peek_key(self) -> dict | None:
        row = self.storage.peek_active_key()
        if not row:
            return None
        return {"id": row["id"], "key": row["key"], "label": row["label"], "source": "local"}

    def _pick_node(self) -> dict | None:
        nodes = self.storage.get_online_nodes()
        if not nodes:
            return None
        with self._rr_lock:
            idx = self._rr_node % len(nodes)
            self._rr_node = (self._rr_node + 1) % max(len(nodes), 1)
        n = nodes[idx]
        self.storage.upsert_node(n["id"], total=n.get("total", 0) + 1)
        return {
            "id": f"node:{n['id']}", "nid": n["id"],
            "proxy_url": n["proxy_url"], "label": f"node-{n['id']}", "source": "node",
        }

    def get_any_key(self, prefer_node: bool = False) -> dict | None:
        if prefer_node:
            route = self._pick_node()
            if route:
                return route
        k = self.get_key()
        if k:
            return k
        return self._pick_node()

    def pick_node(self) -> dict | None:
        return self._pick_node()

    def active_node_count(self) -> int:
        return len(self.storage.get_online_nodes())

    def report_error(self, kid: str, code: int = 429):
        k = self.storage.get_key(kid)
        if not k:
            return
        errors = k["errors"] + 1
        if code == 429:
            backoff = min(30 * (2 ** min(errors - 1, 2)), 120)
        elif code in (403, 503):
            backoff = min(30 * errors, 90)
        else:
            backoff = min(300 * (2 ** min(errors - 1, 4)), 3600)
        updates: dict = {"errors": errors, "cool_until": time.time() + backoff}
        if errors >= 15:
            updates["active"] = 0
        self.storage.update_key(kid, **updates)

    def report_ok(self, kid: str):
        k = self.storage.get_key(kid)
        if not k:
            return
        errors = max(0, k["errors"] - 1)
        self.storage.update_key(kid, errors=errors,
                                 cool_until=0 if errors == 0 else k["cool_until"])

    def report_tokens(self, kid: str, prompt: int, completion: int):
        k = self.storage.get_key(kid)
        if not k:
            return
        self.storage.update_key(kid,
                                  tokens_input=k["tokens_input"] + prompt,
                                  tokens_output=k["tokens_output"] + completion)

    def list_keys(self) -> dict:
        return self.storage.list_keys(include_secret=False)

    def register_node(self, name: str, ip: str, device: str,
                      proxy_url: str, key: str | None = None,
                      node_id: str | None = None) -> tuple[str, str]:
        nid = node_id or str(uuid.uuid4())[:8]
        secret = AUTH_SECRET or "zenpool-default"
        token = generate_node_token(nid, secret)
        valid, _ = validate_proxy_url(proxy_url)
        if not valid:
            proxy_url = f"http://{ip}:{NODE_PORT}"
        self.storage.upsert_node(nid, name=name, ip=ip, device=device,
                                   proxy_url=proxy_url, token=token,
                                   key=key or "", seen=time.time())
        return nid, token

    def heartbeat(self, nid: str, in_flight: int = 0, uptime: int = 0,
                  tokens_proxied: int = 0, public_ip: str = ""):
        kwargs: dict = {"seen": time.time(), "in_flight": in_flight,
                        "uptime": uptime, "tokens_proxied": tokens_proxied}
        if public_ip:
            kwargs["public_ip"] = public_ip
        self.storage.upsert_node(nid, **kwargs)

    def node_online(self, nid: str) -> bool:
        n = self.storage.get_node(nid)
        return bool(n and time.time() - n.get("seen", 0) < 120)

    def verify_node(self, nid: str, token: str) -> bool:
        n = self.storage.get_node(nid)
        return bool(n and n.get("token") == token)

    def list_nodes(self) -> dict:
        now = time.time()
        return {nid: {
            "name": n.get("name", "?"), "ip": n.get("ip", "?"),
            "device": n.get("device", "?"),
            "online": now - n.get("seen", 0) < 60,
            "public_ip": n.get("public_ip", ""),
            "in_flight": n.get("in_flight", 0),
            "uptime": n.get("uptime", 0),
            "tokens_proxied": n.get("tokens_proxied", 0),
        } for nid, n in self.storage.list_nodes().items()}

    def remove_node(self, nid: str) -> bool:
        if not self.storage.get_node(nid):
            return False
        self.storage.remove_node(nid)
        return True

    def prune(self, timeout: int = 90) -> list[str]:
        return self.storage.prune_nodes(timeout)


# ─── WorkQueue ───────────────────────────────────────────────────────

class WorkQueue:
    """Assign requests to nodes via hub polling (NAT-safe)."""

    def __init__(self):
        self._pending: dict = {}
        self._lock = threading.Lock()

    def dispatch(self, body: dict, nid: str, timeout: int = WORK_TIMEOUT) -> dict | None:
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
            return self._pending.pop(req_id, None)

    def poll(self, nid: str) -> dict | None:
        with self._lock:
            for req_id, item in self._pending.items():
                if item["nid"] == nid and not item["claimed"]:
                    item["claimed"] = True
                    return {"request_id": req_id, "body": item["body"]}
        return None

    def complete(self, req_id: str, status: int, result,
                 headers: dict | None = None) -> bool:
        with self._lock:
            item = self._pending.get(req_id)
            if not item:
                return False
            item["status"] = status
            item["result"] = result if isinstance(result, bytes) else str(result).encode()
            item["headers"] = headers or {}
            item["event"].set()
        return True

    def cancel_for_node(self, nid: str):
        with self._lock:
            for item in self._pending.values():
                if item["nid"] == nid and not item["event"].is_set():
                    item["status"] = 503
                    item["result"] = json.dumps({"error": "node offline"}).encode()
                    item["headers"] = {"Content-Type": "application/json"}
                    item["event"].set()


# ═══════════════════════════════════════════════════════════════════════
#  HUB
# ═══════════════════════════════════════════════════════════════════════

def run_hub():
    storage = Storage(DATA_FILE)
    storage.migrate_json("zenpool-data.json")
    pool = KeyPool(storage)
    work_queue = WorkQueue()
    conn_cache = ConnectionCache()
    global _work_queue_for_shutdown
    _work_queue_for_shutdown = work_queue

    class HubHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _s(self, data, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def _e(self, msg: str, code: int = 400):
            self._s({"error": msg}, code)

        def _auth(self) -> bool:
            if not AUTH_SECRET or not REQUIRE_AUTH:
                return True
            if self.headers.get("Authorization", "") == f"Bearer {AUTH_SECRET}":
                return True
            self._e("unauthorized", 401)
            return False

        def _node_auth(self, nid: str | None, token: str) -> bool:
            if not nid or not AUTH_SECRET:
                return True
            if pool.verify_node(nid, token):
                return True
            self._e("invalid node token", 403)
            return False

        def _body(self) -> dict | None:
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except (ValueError, TypeError):
                n = 0
            if n > MAX_BODY:
                self._e("request body too large", 413)
                return None
            return json.loads(self.rfile.read(min(n, MAX_BODY))) if n else {}

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.end_headers()

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/health":
                nodes = storage.get_online_nodes()
                self._s({
                    "ok": True, "version": VERSION, "host": platform.node(),
                    "keys": storage.key_count(),
                    "online_nodes": len(nodes),
                    "total_in_flight": sum(n.get("in_flight", 0) for n in nodes),
                    "total_tokens_proxied": sum(n.get("tokens_proxied", 0) for n in nodes),
                })
            elif p == "/v1/models":
                self._s({"object": "list", "data": [
                    {"id": "big-pickle", "object": "model", "owned_by": "opencode"},
                    {"id": "deepseek-v4-flash-free", "object": "model", "owned_by": "opencode"},
                    {"id": "deepseek-v4-pro", "object": "model", "owned_by": "opencode"},
                    {"id": "nemotron-3-super-free", "object": "model", "owned_by": "opencode"},
                    {"id": "mimo-v2.5-free", "object": "model", "owned_by": "opencode"},
                ]})
            elif p == "/keys":
                if not self._auth():
                    return
                self._s({"keys": pool.list_keys()})
            elif p == "/nodes":
                if not self._auth():
                    return
                self._s({"nodes": pool.list_nodes()})
            elif p == "/metrics":
                self._metrics()
            elif p == "/status":
                self._status()
            else:
                self._e("not found", 404)

        def do_POST(self):
            p = self.path.split("?")[0]

            # /register and proxy are open; everything else requires auth
            if p not in ("/register", "/v1/chat/completions",
                         "/heartbeat", "/poll-work", "/complete-work"):
                if not self._auth():
                    return

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
                proxy_url = b.get("proxy_url", "")
                valid, _ = validate_proxy_url(proxy_url)
                if not valid:
                    proxy_url = f"http://{self.client_address[0]}:{NODE_PORT}"
                nid, token = pool.register_node(
                    b.get("name", f"node-{storage.node_count()+1}"),
                    self.client_address[0],
                    b.get("device", "unknown"),
                    proxy_url,
                    key=b.get("key"),
                    node_id=b.get("node_id"),
                )
                self._s({"node_id": nid, "token": token, "interval": HEARTBEAT_INTERVAL})

            elif p == "/heartbeat":
                nid = b.get("node_id")
                if not self._node_auth(nid, b.get("token", "")):
                    return
                pool.heartbeat(nid,
                               in_flight=b.get("in_flight", 0),
                               uptime=b.get("uptime", 0),
                               tokens_proxied=b.get("tokens_proxied", 0),
                               public_ip=b.get("public_ip", ""))
                self._s({"ok": True})

            elif p == "/poll-work":
                nid = b.get("node_id")
                if not self._node_auth(nid, b.get("token", "")):
                    return
                self._s(work_queue.poll(nid) or {"work": None})

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
                if not self._node_auth(nid, b.get("token", "")):
                    return
                k = pool.get_key_for_node()
                self._s(k if k else {"error": "no keys available"}, 200 if k else 503)

            elif p == "/report":
                kid = b.get("key_id")
                if b.get("ok", True):
                    pool.report_ok(kid)
                else:
                    pool.report_error(kid)
                self._s({"ok": True})

            elif p.startswith("/keys/") and p.endswith("/reactivate"):
                kid = p.split("/")[2]
                k = storage.get_key(kid)
                if not k:
                    return self._e("key not found", 404)
                storage.update_key(kid, active=1, errors=0, cool_until=0)
                self._s({"ok": True})

            elif p == "/v1/chat/completions":
                self._proxy(b)

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

        def _forward_to_node(self, body: dict, k: dict, timeout: int = PUSH_TIMEOUT):
            proxy_url = k["proxy_url"]
            valid, err = validate_proxy_url(proxy_url)
            if not valid:
                raise ValueError(f"SSRF blocked: {err}")
            url = f"{proxy_url.rstrip('/')}/v1/chat/completions"
            parsed = urllib.parse.urlparse(url)
            host = parsed.hostname or "localhost"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "X-ZenPool-Hub": "1", "User-Agent": "curl/7.76.1"},
            )
            with conn_cache.get_opener(host, port).open(req, timeout=timeout) as r:
                self._relay_response(r)

        def _dispatch_via_node(self, body: dict, k: dict) -> bool:
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
            except (urllib.error.URLError, OSError, TimeoutError, ValueError):
                return False

        def _proxy(self, body: dict):
            tried_local: set = set()
            node_attempts = 0
            n_nodes = pool.active_node_count()
            n_keys = storage.key_count()
            max_node_attempts = max(n_nodes * 5, 5)
            max_tries = max(n_keys + n_nodes, 1) * 3
            last_resp = None
            last_code = 503
            prefer_node = n_nodes > 0 and not pool.peek_key()

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

                parsed = urllib.parse.urlparse(ZEN_API)
                zhost = parsed.hostname or "opencode.ai"
                zport = parsed.port or 443
                req = urllib.request.Request(
                    ZEN_API, data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {k['key']}",
                             "User-Agent": "curl/7.76.1"},
                )
                try:
                    with conn_cache.get_opener(zhost, zport).open(req, timeout=120) as r:
                        data = r.read()
                        try:
                            usage = json.loads(data).get("usage", {}) or {}
                            pt, ct = usage.get("prompt_tokens"), usage.get("completion_tokens")
                            if pt is not None and ct is not None:
                                pool.report_tokens(kid, pt, ct)
                        except (json.JSONDecodeError, AttributeError):
                            pass
                        pool.report_ok(kid)
                        self.send_response(r.status)
                        self.send_header("Content-Type",
                                          r.headers.get("Content-Type", "application/json"))
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        self.wfile.write(data)
                        return
                except urllib.error.HTTPError as e:
                    raw = b""
                    try:
                        raw = e.read()
                        resp_body = json.loads(raw) if raw else {"error": f"HTTP {e.code}"}
                    except (json.JSONDecodeError, TypeError):
                        resp_body = {"error": f"upstream {e.code}",
                                     "detail": raw.decode(errors="replace")[:200]}
                    last_resp, last_code = resp_body, e.code
                    if e.code in (429, 503, 403):
                        pool.report_error(kid, e.code)
                        prefer_node = pool.active_node_count() > 0
                    elif e.code >= 500:
                        prefer_node = pool.active_node_count() > 0
                    else:
                        return self._s(resp_body, e.code)
                except urllib.error.URLError as e:
                    return self._e(f"upstream unreachable: {e.reason}", 502)
                except Exception as e:
                    return self._e(f"proxy error: {e}", 502)

            if last_resp is not None:
                return self._s(last_resp, last_code)
            return self._e("no keys available", 503)

        def _metrics(self):
            now = time.time()
            keys_raw = storage.list_keys(include_secret=False)
            active = cooled = dead = 0
            for kv in keys_raw.values():
                if not kv.get("active"):
                    dead += 1
                elif kv.get("cool_until", 0) > now:
                    cooled += 1
                else:
                    active += 1
            lines = [
                "# HELP zenpool_keys_total Keys by status",
                "# TYPE zenpool_keys_total gauge",
                f'zenpool_keys_total{{status="active"}} {active}',
                f'zenpool_keys_total{{status="cooled"}} {cooled}',
                f'zenpool_keys_total{{status="dead"}} {dead}',
                "# HELP zenpool_key_errors Total errors per key",
                "# TYPE zenpool_key_errors counter",
            ]
            for kid, kv in keys_raw.items():
                label = kv.get("label", "")
                lines.append(f'zenpool_key_errors{{key_id="{kid}",label="{label}"}} {kv["errors"]}')
            lines += [
                "# HELP zenpool_key_tokens_input Input tokens per key",
                "# TYPE zenpool_key_tokens_input counter",
            ]
            for kid, kv in keys_raw.items():
                lines.append(f'zenpool_key_tokens_input{{key_id="{kid}"}} {kv["tokens_input"]}')
            lines += [
                "# HELP zenpool_key_tokens_output Output tokens per key",
                "# TYPE zenpool_key_tokens_output counter",
            ]
            for kid, kv in keys_raw.items():
                lines.append(f'zenpool_key_tokens_output{{key_id="{kid}"}} {kv["tokens_output"]}')
            lines += [
                "# HELP zenpool_nodes_online Nodes currently online",
                "# TYPE zenpool_nodes_online gauge",
                f"zenpool_nodes_online {pool.active_node_count()}",
                "# HELP zenpool_uptime_seconds Hub uptime",
                "# TYPE zenpool_uptime_seconds gauge",
                f"zenpool_uptime_seconds {int(time.time() - pool._start)}",
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(("\n".join(lines) + "\n").encode())

        def _status(self):
            now = time.time()
            keys_raw = storage.list_keys(include_secret=False)
            nodes_raw = pool.list_nodes()
            self._s({
                "version": VERSION,
                "uptime": int(now - pool._start),
                "total_requests": sum(v.get("total", 0) for v in keys_raw.values()),
                "total_tokens": sum(
                    v.get("tokens_input", 0) + v.get("tokens_output", 0)
                    for v in keys_raw.values()),
                "active_keys": sum(1 for v in keys_raw.values()
                                    if v.get("active") and not v.get("cool")),
                "cooled_keys": sum(1 for v in keys_raw.values() if v.get("cool")),
                "dead_keys": sum(1 for v in keys_raw.values() if not v.get("active")),
                "online_nodes": sum(1 for n in nodes_raw.values() if n.get("online")),
                "total_nodes": len(nodes_raw),
                "keys": keys_raw,
                "nodes": nodes_raw,
            })

    def _prune():
        while True:
            time.sleep(15)
            for nid in pool.prune():
                work_queue.cancel_for_node(nid)

    threading.Thread(target=_prune, daemon=True).start()

    print(f"\n  🐍 ZenPool Hub v{VERSION}  —  {platform.node()}")
    print(f"  ├─ Port:   {HUB_PORT}")
    print(f"  ├─ Keys:   {storage.key_count()}")
    print(f"  ├─ DB:     {os.path.abspath(DATA_FILE)}")
    print(f"  ├─ Auth:   {'required' if AUTH_SECRET and REQUIRE_AUTH else 'open'}")
    print("  └─ SSRF:   protected")
    print()
    print("  Endpoints:")
    print("    GET  /health  /status  /metrics")
    print("    GET  /keys    /nodes                    (auth required)")
    print("    POST /keys    DELETE /keys/<id>")
    print("    POST /register  /heartbeat  /next-key  /report")
    print("    POST /poll-work  /complete-work")
    print("    POST /keys/<id>/reactivate")
    print("    POST /v1/chat/completions   GET /v1/models")
    print()
    print(f"  Nodes: curl -fsSL <url>/zenpool.py | python3 - node --hub http://<ip>:{HUB_PORT}")
    print()
    ThreadingHTTPServer(("0.0.0.0", HUB_PORT), HubHandler).serve_forever()


# ═══════════════════════════════════════════════════════════════════════
#  NODE
# ═══════════════════════════════════════════════════════════════════════

class NodeClient:
    def __init__(self, hub: str, local_key: str | None = None,
                 proxy_url: str | None = None, state_dir: str | None = None,
                 max_workers: int = 0):
        self.hub = hub.rstrip("/")
        self.local_key = local_key
        self.proxy_url = proxy_url or os.environ.get("ZENPOOL_PUBLIC_URL")
        self.max_workers = max_workers or int(os.environ.get("ZENPOOL_NODE_MAX_WORKERS", "8"))
        self._work_semaphore = threading.BoundedSemaphore(self.max_workers)
        self.state_dir = state_dir or os.environ.get("ZENPOOL_STATE", _default_state_dir())
        self.state_file = os.path.join(self.state_dir, "node-state.json")
        self.nid, self.token = self._load_state()
        self.hub_ok = False
        self.name = platform.node() or "unknown"
        self.device = f"{platform.system()}/{platform.machine()}"
        self._conn_cache = ConnectionCache()
        self.start_time = time.time()
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()
        self.tokens_proxied = 0
        self.public_ip = "unknown"

    def _load_state(self) -> tuple[str | None, str | None]:
        try:
            with open(self.state_file) as f:
                d = json.load(f)
                return d.get("node_id"), d.get("token")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None, None

    def _save_state(self):
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump({"node_id": self.nid, "token": self.token,
                           "hub": self.hub, "public_ip": self.public_ip}, f)
        except OSError:
            pass

    def _call(self, path: str, data: dict | None = None) -> dict:
        body = json.dumps(data).encode() if data else None
        try:
            parsed = urllib.parse.urlparse(self.hub)
            host = parsed.hostname or "localhost"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            r = self._conn_cache.get_opener(host, port).open(
                urllib.request.Request(
                    f"{self.hub}{path}", data=body,
                    headers={"Content-Type": "application/json"}),
                timeout=10,
            )
            return json.loads(r.read())
        except Exception as e:
            return {"error": str(e)}

    def register(self) -> bool:
        payload: dict = {"name": self.name, "device": self.device}
        if self.nid:
            payload["node_id"] = self.nid
        if self.local_key:
            payload["key"] = self.local_key
        if self.proxy_url:
            payload["proxy_url"] = self.proxy_url.rstrip("/")
        r = self._call("/register", payload)
        if r.get("node_id"):
            self.nid = r["node_id"]
            self.token = r.get("token", "")
            self.hub_ok = True
            self._save_state()
            return True
        self.hub_ok = False
        return False

    def heartbeat(self):
        with self._in_flight_lock:
            in_flight = self._in_flight
        r = self._call("/heartbeat", {
            "node_id": self.nid, "token": self.token or "",
            "in_flight": in_flight,
            "uptime": int(time.time() - self.start_time),
            "tokens_proxied": self.tokens_proxied,
            "public_ip": self.public_ip,
        })
        self.hub_ok = bool(r.get("ok")) and not r.get("error")

    def poll_work(self) -> dict:
        return self._call("/poll-work", {"node_id": self.nid, "token": self.token or ""})

    def complete_work(self, request_id: str, status: int, body,
                      headers: dict | None = None):
        self._call("/complete-work", {
            "request_id": request_id, "status": status,
            "body": body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body,
            "headers": headers or {},
        })

    def next_key(self) -> dict | None:
        r = self._call("/next-key", {"node_id": self.nid, "token": self.token or ""})
        return r if r.get("key") else None

    def report(self, kid: str, ok: bool = True):
        self._call("/report", {"key_id": kid, "ok": ok, "node_id": self.nid})

    def run_hub_work(self, work: dict):
        with self._in_flight_lock:
            self._in_flight += 1
        try:
            body = work.get("body")
            req_id = work.get("request_id")
            if not body or not req_id:
                return
            key = {"key": self.local_key, "id": None} if self.local_key else self.next_key()
            if not key or not key.get("key"):
                self.complete_work(req_id, 503,
                                   json.dumps({"error": "no keys available"}),
                                   {"Content-Type": "application/json"})
                return
            parsed = urllib.parse.urlparse(ZEN_API)
            req = urllib.request.Request(
                ZEN_API, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "User-Agent": "curl/7.76.1",
                         "Authorization": f"Bearer {key['key']}"},
            )
            try:
                with self._conn_cache.get_opener(
                        parsed.hostname, parsed.port or 443).open(req, timeout=WORK_TIMEOUT) as r:
                    data = r.read()
                    try:
                        tt = ((json.loads(data).get("usage") or {})
                              .get("total_tokens", 0) or 0)
                        self.tokens_proxied += tt
                    except (json.JSONDecodeError, AttributeError):
                        pass
                    if key.get("id"):
                        self.report(key["id"], ok=True)
                    self.complete_work(req_id, r.status, data,
                                       {"Content-Type": r.headers.get(
                                           "Content-Type", "application/json")})
            except urllib.error.HTTPError as e:
                if key.get("id"):
                    self.report(key["id"], ok=False)
                self.complete_work(req_id, e.code, e.read(),
                                   {"Content-Type": "application/json"})
            except Exception as e:
                self.complete_work(req_id, 502,
                                   json.dumps({"error": str(e)}),
                                   {"Content-Type": "application/json"})
        finally:
            with self._in_flight_lock:
                self._in_flight -= 1
            self._work_semaphore.release()


def run_node(hub_url: str, local_key: str | None = None,
             proxy_url: str | None = None):
    client = NodeClient(hub_url, local_key=local_key, proxy_url=proxy_url)

    class NodeHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _s(self, data, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def _e(self, msg: str, code: int = 400):
            self._s({"error": msg}, code)

        def _body(self) -> dict:
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
                self._s({
                    "ok": True, "node": client.nid, "hub": client.hub,
                    "registered": client.hub_ok, "version": VERSION,
                    "public_ip": client.public_ip,
                    "uptime": int(time.time() - client.start_time),
                    "in_flight": client._in_flight,
                    "tokens_proxied": client.tokens_proxied,
                })
            elif p in ("/models", "/v1/models"):
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

        def _proxy(self, body: dict):
            key = ({"key": client.local_key, "id": None}
                   if client.local_key else client.next_key())
            headers = {"Content-Type": "application/json", "User-Agent": "curl/7.76.1"}
            if key:
                headers["Authorization"] = f"Bearer {key['key']}"
            parsed = urllib.parse.urlparse(ZEN_API)
            zhost = parsed.hostname or "opencode.ai"
            zport = parsed.port or 443
            req = urllib.request.Request(ZEN_API, data=json.dumps(body).encode(),
                                          headers=headers)
            with client._in_flight_lock:
                client._in_flight += 1
            try:
                with client._conn_cache.get_opener(zhost, zport).open(req, timeout=180) as r:
                    data = r.read()
                    try:
                        tt = ((json.loads(data).get("usage") or {})
                              .get("total_tokens", 0) or 0)
                        client.tokens_proxied += tt
                    except (json.JSONDecodeError, AttributeError):
                        pass
                    if key and key.get("id"):
                        client.report(key["id"], ok=True)
                    self.send_response(r.status)
                    self.send_header("Content-Type",
                                      r.headers.get("Content-Type", "application/json"))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
            except urllib.error.HTTPError as e:
                if key and key.get("id"):
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

    def _loop():
        while True:
            if not client.nid or not client.hub_ok:
                if client.register():
                    print(f"  ✅ Registered with hub: {client.nid}")
                elif client.nid:
                    print(f"  ⚠️  Hub unreachable — retrying (id: {client.nid})")
            else:
                client.heartbeat()
            time.sleep(HEARTBEAT_INTERVAL)

    def _poll_loop():
        while True:
            if client.nid and client.hub_ok:
                work = client.poll_work()
                if work and work.get("request_id"):
                    client._work_semaphore.acquire()
                    threading.Thread(target=client.run_hub_work,
                                     args=(work,), daemon=True).start()
            time.sleep(POLL_INTERVAL)

    def _ip_loop():
        while True:
            try:
                with urllib.request.urlopen(
                    urllib.request.Request("http://checkip.amazonaws.com"),
                    timeout=5,
                ) as r:
                    client.public_ip = r.read().decode().strip()
                client._save_state()
            except Exception:
                pass
            time.sleep(15)

    print(f"\n  🐍 ZenPool Node v{VERSION}  —  {client.name}")
    print(f"  ├─ Hub:     {client.hub}")
    print(f"  ├─ Port:    {NODE_PORT}")
    print(f"  ├─ Workers: {client.max_workers}")
    print(f"  ├─ Device:  {client.device}")
    print(f"  └─ State:   {client.state_dir}")
    print()

    client.register()
    threading.Thread(target=_loop, daemon=True).start()
    threading.Thread(target=_poll_loop, daemon=True).start()
    threading.Thread(target=_ip_loop, daemon=True).start()

    print(f"  🚀 Hub:   {client.hub}/v1/chat/completions")
    print(f"  📡 Local: http://localhost:{NODE_PORT}/v1/chat/completions")
    print()
    try:
        ThreadingHTTPServer(("0.0.0.0", NODE_PORT), NodeHandler).serve_forever()
    except OSError as e:
        if e.errno == 98 or "Address already in use" in str(e):
            print(f"  ❌ Port {NODE_PORT} in use — another node is running.")
            print("     Stop it: pkill -u \"$(id -u)\" -f 'zenpool.py.*node'")
            raise SystemExit(1) from e
        raise


# ═══════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        prog="zenpool",
        description=(
            "ZenPool — distributed API key pool for OpenCode Zen\n"
            "  Linux/Mac:  curl -fsSL <url>/zenpool.py | python3 - node\n"
            "  PowerShell: (Invoke-WebRequest -Uri <url>/zenpool.py).Content | python - node"
        ),
    )
    p.add_argument("mode", choices=["hub", "node"])
    p.add_argument("--hub", default=DEFAULT_HUB,
                   help=f"hub URL for node mode (default: {DEFAULT_HUB})")
    p.add_argument("--key", default=None,
                   help="donate a local API key to the hub pool")
    p.add_argument("--public-url", default=None,
                   help="reachable URL for this node (e.g. http://100.x.x.x:5052 for Tailscale)")

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
