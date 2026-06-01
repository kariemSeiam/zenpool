# ZenPool 🐍

> Pool API keys across any number of machines. One endpoint. Never rate-limited.

```bash
curl -fsSL https://srv880434.hstgr.cloud/zenpool.py | python3 - node
```

---

## How it works

Every request you send hits the **hub**. The hub picks the next fresh key and fires. Zero configuration on your end.

```
you  ──▶  hub  ──▶  OpenCode
```

Under the hood, the hub runs a live key pool with two layers:

**Layer 1 — Local keys**
Keys you added directly to the hub. Served round-robin. When one gets a 429, it cools down and the next one takes over.

**Layer 2 — Node keys** *(automatic fallback)*
Any machine running `zenpool.py node --key sk-xxx` donates that key to the hub's fallback pool. When every local key is cooling, the hub routes requests through an online node — a different IP, a fresh rate-limit window.

**The cooling ladder**

| Hit | Cooldown |
|-----|----------|
| 1st 429 | 30s |
| 2nd | 60s |
| 3rd | 120s |
| 15 errors | key deactivated |

A successful response shaves one error off the count. Keys heal themselves.

**Node lifecycle**
A node registers, sends a heartbeat every 30 seconds, and gets pruned 90 seconds after it goes silent. Its keys leave the pool with it. No manual cleanup.

---

## Install

<details>
<summary><strong>One command — any OS</strong></summary>

```bash
# Join the pool and donate a key
curl -fsSL https://srv880434.hstgr.cloud/zenpool.py | bash -s -- node --key sk-xxxxx

# Run a hub (your server)
curl -fsSL https://srv880434.hstgr.cloud/zenpool.py | sudo bash -s -- hub
```

The script detects your OS and sets up a background service:

| Platform | Service | Survives reboot |
|----------|---------|-----------------|
| Linux (root) | systemd system service | ✅ |
| Linux (user) | systemd user + linger | ✅ |
| macOS | LaunchAgent | ✅ |
| Windows | Scheduled Task | ✅ |
| Termux | `.bashrc` auto-start | ✅ |

</details>

<details>
<summary><strong>Manual</strong></summary>

```bash
# Hub
python3 zenpool.py hub

# Node — connects to default hub
python3 zenpool.py node

# Node — donate a key
python3 zenpool.py node --key sk-your-key-here

# Node — custom hub
python3 zenpool.py node --hub http://192.168.1.10:5051 --key sk-your-key-here

# Node — explicit public URL (Tailscale / NAT)
python3 zenpool.py node --public-url http://100.x.x.x:5052
```

</details>

---

## Use it

Drop the hub URL in as your `base_url`. The `api_key` field is ignored by the hub — it handles key selection internally.

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://srv880434.hstgr.cloud/v1",
    api_key="ignored",
)
```

```bash
curl https://srv880434.hstgr.cloud/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"big-pickle","messages":[{"role":"user","content":"hello"}]}'
```

**Available models**

| Model | Tier |
|-------|------|
| `big-pickle` | Default |
| `deepseek-v4-flash-free` | Free |
| `deepseek-v4-pro` | Pro |
| `nemotron-3-super-free` | Free |
| `mimo-v2.5-free` | Free |

---

## Configuration

All config via environment variables. Nothing is required to run.

| Variable | Default | What it does |
|----------|---------|--------------|
| `ZENPOOL_HUB` | `https://srv880434.hstgr.cloud` | Hub URL (node mode) |
| `ZENPOOL_PORT` | `5051` | Hub listen port |
| `ZENPOOL_NODE_PORT` | `5052` | Node listen port |
| `ZENPOOL_DATA` | `zenpool.db` | SQLite path (hub) |
| `ZENPOOL_SECRET` | *(empty)* | Enables auth when set |
| `ZENPOOL_REQUIRE_AUTH` | `1` | Enforce auth on key/node endpoints |
| `ZENPOOL_MAX_BODY` | `10485760` | Max request body (bytes) |
| `ZENPOOL_NODE_MAX_WORKERS` | `8` | Parallel work threads per node |
| `ZENPOOL_PUBLIC_URL` | *(auto-detected)* | Node's reachable URL |
| `ZENPOOL_STATE` | platform state dir | Node state directory |

---

## API reference

<details>
<summary><strong>Observability</strong> (open)</summary>

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Status, key/node counts, tokens in-flight |
| `GET` | `/status` | Full detail — per-key + per-node stats |
| `GET` | `/metrics` | Prometheus text format |
| `GET` | `/v1/models` | Model list |

</details>

<details>
<summary><strong>Proxy</strong> (open)</summary>

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/chat/completions` | Main proxy — hub picks the key |

</details>

<details>
<summary><strong>Key management</strong> (requires secret)</summary>

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/keys` | List all keys (values masked) |
| `POST` | `/keys` | Add a key — `{"key": "sk-...", "label": ""}` |
| `DELETE` | `/keys/<id>` | Remove a key |
| `POST` | `/keys/<id>/reactivate` | Re-enable a deactivated key |

</details>

<details>
<summary><strong>Node management</strong> (requires secret)</summary>

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/nodes` | List all nodes |
| `DELETE` | `/nodes/<id>` | Remove a node |

</details>

<details>
<summary><strong>Node protocol</strong> (requires node token)</summary>

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/register` | Register node — returns `node_id` + `token` |
| `POST` | `/heartbeat` | Keep-alive (30s interval) |
| `POST` | `/next-key` | Request a key from the hub pool |
| `POST` | `/report` | Report key success/failure |
| `POST` | `/poll-work` | Pull hub-assigned work (NAT-safe) |
| `POST` | `/complete-work` | Return completed work result |

Auth header: `Authorization: Bearer <ZENPOOL_SECRET>`

</details>

---

## Security

**SSRF protection** — node proxy URLs are validated against RFC-1918 and loopback ranges before any connection is made. A node cannot be registered pointing at `localhost` or an internal IP.

**Key masking** — API keys are never returned in full via any endpoint. All responses show `sk-ab...cdef`.

**Node tokens** — registration returns a per-node HMAC token. Every subsequent hub call (heartbeat, poll-work, next-key) must present it. Tokens are tied to the node ID and the shared secret.

**Auth enforcement** — set `ZENPOOL_SECRET` to require `Authorization: Bearer <secret>` on all key and node management endpoints.

---

## Reliability

**SQLite WAL mode** — concurrent reads never block writes. Journal mode survives hard kills cleanly.

**Crash recovery** — node state (ID + token) is written to disk. A node that restarts re-registers with its existing ID and picks up where it left off.

**Migration** — if `zenpool-data.json` exists from a v2 deployment, it's automatically imported into the SQLite database on first start and renamed to `.migrated`.

**Public IP tracking** — each node pings `checkip.amazonaws.com` every 15 seconds and stores its public IP. The hub uses this for direct push routing.

---

## Requirements

Python 3.10+, stdlib only. No `pip install`. `sqlite3` ships with Python.

---

🐍🐙∞
