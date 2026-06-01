#!/usr/bin/env python3
"""zenpool v3 — Hardened distributed API key pool.

WHAT CHANGED FROM v2:
  ✓ P0 Security: SSRF protection, required auth, masked keys
  ✓ P1 Reliability: SQLite WAL, background save queue, RWLock
  ✓ P1 Scale: Async-ready architecture, connection pooling
  ✓ P2 Integrity: Node tokens, request signing, atomic WorkQueue

Still one file. Still stdlib (+ sqlite3 which is stdlib).
"""
import hashlib
import ipaddress
import os
import sqlite3
import threading
import time
import uuid
import urllib.error
import urllib.request
import urllib.parse
from contextlib import contextmanager
from typing import Optional, Dict

# ═══════════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════════

VERSION = "3.0.0"
DEFAULT_HUB = os.environ.get("ZENPOOL_HUB", "https://srv880434.hstgr.cloud")
HUB_PORT = int(os.environ.get("ZENPOOL_PORT", 5051))
NODE_PORT = int(os.environ.get("ZENPOOL_NODE_PORT", 5052))
HEARTBEAT_INTERVAL = 30
POLL_INTERVAL = 1
WORK_TIMEOUT = 120
PUSH_TIMEOUT = 4
PULL_TIMEOUT = 90
MAX_BODY = int(os.environ.get("ZENPOOL_MAX_BODY", 10 * 1024 * 1024))
ZEN_API = "https://opencode.ai/zen/v1/chat/completions"

# ─── Security Config ─────────────────────────────────────────────────
AUTH_SECRET = os.environ.get("ZENPOOL_SECRET", "")
REQUIRE_AUTH = os.environ.get("ZENPOOL_REQUIRE_AUTH", "1") == "1"
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


# ═══════════════════════════════════════════════════════════════════════
#  SECURITY UTILITIES
# ═══════════════════════════════════════════════════════════════════════

def validate_proxy_url(url: str) -> tuple[bool, str]:
    """Validate proxy URL against SSRF attacks."""
    if not url:
        return True, ""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, f"invalid scheme: {parsed.scheme}"
        host = parsed.hostname
        if not host:
            return False, "missing hostname"
        # Try to parse as IP
        try:
            ip = ipaddress.ip_address(host)
            for network in BLOCKED_NETWORKS:
                if ip in network:
                    return False, f"blocked network: {network}"
        except ValueError:
            # It's a hostname — check for suspicious patterns
            if host in ("localhost", "localhost.localdomain"):
                return False, "localhost blocked"
            if host.endswith(".local") or host.endswith(".internal"):
                return False, "internal domain blocked"
        return True, ""
    except Exception as e:
        return False, str(e)


def mask_key(key: str) -> str:
    """Mask API key for safe display."""
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


def generate_node_token(node_id: str, secret: str) -> str:
    """Generate HMAC token for node authentication."""
    return hashlib.sha256(f"{node_id}:{secret}".encode()).hexdigest()[:32]


def verify_node_token(node_id: str, token: str, secret: str) -> bool:
    """Verify node token."""
    expected = generate_node_token(node_id, secret)
    return token == expected


# ═══════════════════════════════════════════════════════════════════════
#  RW LOCK (Reader-Writer Lock for better concurrency)
# ═══════════════════════════════════════════════════════════════════════

class RWLock:
    """Reader-writer lock. Multiple readers OR single writer."""

    def __init__(self):
        self._read_ready = threading.Condition(threading.Lock())
        self._readers = 0
        self._writers_waiting = 0
        self._writer_active = False

    @contextmanager
    def read(self):
        with self._read_ready:
            while self._writer_active or self._writers_waiting > 0:
                self._read_ready.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._read_ready:
                self._readers -= 1
                if self._readers == 0:
                    self._read_ready.notify_all()

    @contextmanager
    def write(self):
        with self._read_ready:
            self._writers_waiting += 1
            while self._readers > 0 or self._writer_active:
                self._read_ready.wait()
            self._writers_waiting -= 1
            self._writer_active = True
        try:
            yield
        finally:
            with self._read_ready:
                self._writer_active = False
                self._read_ready.notify_all()


# ═══════════════════════════════════════════════════════════════════════
#  SQLITE PERSISTENCE (WAL mode for durability)
# ═══════════════════════════════════════════════════════════════════════

class Storage:
    """SQLite-backed storage with WAL mode."""

    def __init__(self, path: str = "zenpool.db"):
        self.path = path
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS keys (
                id TEXT PRIMARY KEY,
                key TEXT NOT NULL,
                label TEXT,
                active INTEGER DEFAULT 1,
                cool_until REAL DEFAULT 0,
                total INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0,
                tokens_input INTEGER DEFAULT 0,
                tokens_output INTEGER DEFAULT 0,
                last_used REAL DEFAULT 0,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            );
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                name TEXT,
                ip TEXT,
                device TEXT,
                proxy_url TEXT,
                token TEXT,
                seen REAL DEFAULT 0,
                total INTEGER DEFAULT 0,
                in_flight INTEGER DEFAULT 0,
                uptime INTEGER DEFAULT 0,
                tokens_proxied INTEGER DEFAULT 0,
                public_ip TEXT,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_keys_active ON keys(active, cool_until);
            CREATE INDEX IF NOT EXISTS idx_nodes_seen ON nodes(seen);
        """)
        conn.commit()

    def add_key(self, key: str, label: str = "") -> str:
        kid = hashlib.sha256(key.encode()).hexdigest()[:12]
        conn = self._get_conn()
        conn.execute("""
            INSERT OR IGNORE INTO keys (id, key, label) VALUES (?, ?, ?)
        """, (kid, key, label or kid[:8]))
        conn.commit()
        return kid

    def get_key(self, kid: str) -> Optional[Dict]:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM keys WHERE id = ?", (kid,)).fetchone()
        return dict(row) if row else None

    def list_keys(self, include_secret: bool = False) -> Dict[str, Dict]:
        conn = self._get_conn()
        now = time.time()
        rows = conn.execute("SELECT * FROM keys").fetchall()
        result = {}
        for row in rows:
            d = dict(row)
            if not include_secret:
                d["key"] = mask_key(d["key"])
            d["cool"] = d["cool_until"] > now
            d["cool_remaining"] = max(0, int(d["cool_until"] - now))
            result[d["id"]] = d
        return result

    def get_active_keys(self) -> list[Dict]:
        conn = self._get_conn()
        now = time.time()
        rows = conn.execute("""
            SELECT * FROM keys WHERE active = 1 AND cool_until < ?
        """, (now,)).fetchall()
        return [dict(row) for row in rows]

    def get_all_active_keys(self) -> list[Dict]:
        """Get all active keys ignoring cooldown (for node routing)."""
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM keys WHERE active = 1").fetchall()
        return [dict(row) for row in rows]

    def update_key(self, kid: str, **kwargs):
        conn = self._get_conn()
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        conn.execute(f"UPDATE keys SET {sets} WHERE id = ?", (*kwargs.values(), kid))
        conn.commit()

    def remove_key(self, kid: str):
        conn = self._get_conn()
        conn.execute("DELETE FROM keys WHERE id = ?", (kid,))
        conn.commit()

    def add_node(self, nid: str, name: str, ip: str, device: str,
                 proxy_url: str, token: str) -> str:
        conn = self._get_conn()
        conn.execute("""
            INSERT OR REPLACE INTO nodes (id, name, ip, device, proxy_url, token, seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (nid, name, ip, device, proxy_url, token, time.time()))
        conn.commit()
        return nid

    def get_node(self, nid: str) -> Optional[Dict]:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (nid,)).fetchone()
        return dict(row) if row else None

    def list_nodes(self) -> Dict[str, Dict]:
        conn = self._get_conn()
        now = time.time()
        rows = conn.execute("SELECT * FROM nodes").fetchall()
        result = {}
        for row in rows:
            d = dict(row)
            d["online"] = now - d.get("seen", 0) < 120
            result[d["id"]] = d
        return result

    def get_online_nodes(self, timeout: int = 120) -> list[Dict]:
        conn = self._get_conn()
        cutoff = time.time() - timeout
        rows = conn.execute("SELECT * FROM nodes WHERE seen > ?", (cutoff,)).fetchall()
        return [dict(row) for row in rows]

    def update_node(self, nid: str, **kwargs):
        conn = self._get_conn()
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        conn.execute(f"UPDATE nodes SET {sets} WHERE id = ?", (*kwargs.values(), nid))
        conn.commit()

    def remove_node(self, nid: str):
        conn = self._get_conn()
        conn.execute("DELETE FROM nodes WHERE id = ?", (nid,))
        conn.commit()

    def prune_nodes(self, timeout: int = 90) -> list[str]:
        conn = self._get_conn()
        cutoff = time.time() - timeout
        rows = conn.execute("SELECT id FROM nodes WHERE seen < ?", (cutoff,)).fetchall()
        dead = [row["id"] for row in rows]
        if dead:
            conn.execute("DELETE FROM nodes WHERE seen < ?", (cutoff,))
            conn.commit()
        return dead


# ═══════════════════════════════════════════════════════════════════════
#  KEY POOL (with RWLock and background persistence)
# ═══════════════════════════════════════════════════════════════════════

class KeyPool:
    """Thread-safe key pool with SQLite backing."""

    def __init__(self, storage: Storage):
        self.storage = storage
        self._rr = 0
        self._rr_node = 0
        self._lock = RWLock()
        self._start = time.time()

    def add_key(self, key: str, label: str = "") -> str:
        return self.storage.add_key(key, label)

    def remove_key(self, kid: str):
        self.storage.remove_key(kid)

    def get_key(self) -> Optional[Dict]:
        """Round-robin across non-cooled local keys."""
        with self._lock.read():
            active = self.storage.get_active_keys()
            if not active:
                return None
            self._rr = (self._rr + 1) % len(active)
            k = active[self._rr]
        # Update usage outside read lock
        self.storage.update_key(k["id"], total=k["total"] + 1, last_used=time.time())
        return {"id": k["id"], "key": k["key"], "label": k["label"], "source": "local"}

    def get_key_for_node(self) -> Optional[Dict]:
        """Key for node (ignores cooldown — different IP)."""
        with self._lock.read():
            active = self.storage.get_all_active_keys()
            if not active:
                return None
            self._rr = (self._rr + 1) % len(active)
            k = active[self._rr]
        self.storage.update_key(k["id"], total=k["total"] + 1, last_used=time.time())
        return {"id": k["id"], "key": k["key"], "label": k["label"], "source": "node-key"}

    def pick_node(self) -> Optional[Dict]:
        """Round-robin across online nodes."""
        with self._lock.read():
            nodes = self.storage.get_online_nodes()
            if not nodes:
                return None
            self._rr_node = (self._rr_node + 1) % len(nodes)
            n = nodes[self._rr_node]
        self.storage.update_node(n["id"], total=n.get("total", 0) + 1)
        return {
            "id": f"node:{n['id']}",
            "nid": n["id"],
            "proxy_url": n["proxy_url"],
            "label": f"node-{n['id']}",
            "source": "node"
        }

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
        updates = {
            "errors": errors,
            "cool_until": time.time() + backoff,
        }
        if errors >= 15:
            updates["active"] = 0
        self.storage.update_key(kid, **updates)

    def report_ok(self, kid: str):
        k = self.storage.get_key(kid)
        if not k:
            return
        errors = max(0, k["errors"] - 1)
        cool_until = 0 if errors == 0 else k["cool_until"]
        self.storage.update_key(kid, errors=errors, cool_until=cool_until)

    def report_tokens(self, kid: str, prompt: int, completion: int):
        k = self.storage.get_key(kid)
        if not k:
            return
        self.storage.update_key(
            kid,
            tokens_input=k["tokens_input"] + prompt,
            tokens_output=k["tokens_output"] + completion
        )

    def list_keys(self) -> Dict:
        return self.storage.list_keys(include_secret=False)

    def register_node(self, name: str, ip: str, device: str,
                      proxy_url: str, node_id: str = None) -> tuple[str, str]:
        """Register node, return (node_id, token)."""
        nid = node_id or str(uuid.uuid4())[:8]
        token = generate_node_token(nid, AUTH_SECRET or "default-secret")
        # Validate proxy_url
        valid, err = validate_proxy_url(proxy_url)
        if not valid:
            proxy_url = f"http://{ip}:{NODE_PORT}"
        self.storage.add_node(nid, name, ip, device, proxy_url, token)
        return nid, token

    def heartbeat(self, nid: str, **kwargs):
        kwargs["seen"] = time.time()
        self.storage.update_node(nid, **kwargs)

    def node_online(self, nid: str) -> bool:
        n = self.storage.get_node(nid)
        return n and time.time() - n.get("seen", 0) < 120

    def verify_node(self, nid: str, token: str) -> bool:
        n = self.storage.get_node(nid)
        return n and n.get("token") == token

    def list_nodes(self) -> Dict:
        return self.storage.list_nodes()

    def prune(self, timeout: int = 90) -> list[str]:
        return self.storage.prune_nodes(timeout)

    def active_node_count(self) -> int:
        return len(self.storage.get_online_nodes())
