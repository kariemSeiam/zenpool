# ZenPool 🐍

Distributed API key pool for OpenCode Zen. Pool keys across any number of machines — never hit a rate limit again.

```bash
curl -fsSL https://srv880434.hstgr.cloud/zenpool.py | python3 - node
```

---

## How it works

```
  Your client
      │
      ▼
  ┌───────────────────────────────────────┐
  │  HUB  https://srv880434.hstgr.cloud  │
  │                                       │
  │  local keys → round-robin             │
  │  node keys  → fallback pool           │
  │  rate-limited key → exponential cool  │
  └───────────────┬───────────────────────┘
                  │
      ┌───────────┼───────────┐
      ▼           ▼           ▼
   Node A      Node B      Node C
  --key sk-1  --key sk-2  (borrows)
```

- Hub checks local keys first (round-robin across non-cooled)
- If all local keys are cooling → routes through an online node
- 429 response → key enters exponential backoff (30s → 60s → 120s)
- Node dies → hub removes it and its keys within 90s
- All persistence: SQLite WAL — survives hard restarts

---

## Install

### One command (any OS)

```bash
# Node — joins the pool, optionally donates a key
curl -fsSL https://srv880434.hstgr.cloud/zenpool.py | bash -s -- node --key sk-xxxxx

# Hub — manages the pool (run on your server)
curl -fsSL https://srv880434.hstgr.cloud/zenpool.py | sudo bash -s -- hub
```

The install script sets up a background service automatically:

| Platform        | Service type              | Survives reboot |
|-----------------|---------------------------|-----------------|
| Linux (root)    | systemd system service    | ✅              |
| Linux (user)    | systemd user + linger     | ✅              |
| macOS           | LaunchAgent               | ✅              |
| Windows         | Scheduled Task            | ✅              |
| Termux          | `.bashrc` auto-start      | ✅              |

### Manual

```bash
# Hub
python3 zenpool.py hub

# Node (connects to default hub)
python3 zenpool.py node

# Node with key donation
python3 zenpool.py node --key sk-your-key-here

# Node pointing at a custom hub
python3 zenpool.py node --hub http://192.168.1.10:5051 --key sk-your-key-here

# Node with explicit public URL (for Tailscale / NAT traversal)
python3 zenpool.py node --public-url http://100.x.x.x:5052
```

---

## Use it

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://srv880434.hstgr.cloud/v1",
    api_key="ignored",
)

response = client.chat.completions.create(
    model="big-pickle",
    messages=[{"role": "user", "content": "hello"}],
)
```

```bash
curl https://srv880434.hstgr.cloud/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"big-pickle","messages":[{"role":"user","content":"hi"}]}'
```

---

## Environment variables

| Variable                  | Default                              | Description                          |
|---------------------------|--------------------------------------|--------------------------------------|
| `ZENPOOL_HUB`             | `https://srv880434.hstgr.cloud`      | Hub URL (node mode)                  |
| `ZENPOOL_PORT`            | `5051`                               | Hub listen port                      |
| `ZENPOOL_NODE_PORT`       | `5052`                               | Node listen port                     |
| `ZENPOOL_DATA`            | `zenpool.db`                         | SQLite database path (hub)           |
| `ZENPOOL_SECRET`          | *(empty)*                            | Shared secret — enables auth         |
| `ZENPOOL_REQUIRE_AUTH`    | `1`                                  | Enforce auth when secret is set      |
| `ZENPOOL_MAX_BODY`        | `10485760`                           | Max request body size (bytes)        |
| `ZENPOOL_NODE_MAX_WORKERS`| `8`                                  | Concurrent hub-work threads per node |
| `ZENPOOL_PUBLIC_URL`      | *(auto)*                             | Node's reachable URL                 |
| `ZENPOOL_STATE`           | platform state dir                   | Node state directory                 |

---

## Hub API

| Method   | Path                        | Auth   | Description                                    |
|----------|-----------------------------|--------|------------------------------------------------|
| `GET`    | `/health`                   | open   | Hub status, key/node counts, tokens in-flight  |
| `GET`    | `/status`                   | open   | Full detail: per-key stats, per-node stats     |
| `GET`    | `/metrics`                  | open   | Prometheus text format                         |
| `GET`    | `/v1/models`                | open   | Available models list                          |
| `POST`   | `/v1/chat/completions`      | open   | **Main proxy endpoint**                        |
| `GET`    | `/keys`                     | secret | All keys (masked) + cooldown state             |
| `POST`   | `/keys`                     | secret | Add key — body: `{"key": "sk-...", "label": ""}` |
| `DELETE` | `/keys/<id>`                | secret | Remove a key                                   |
| `POST`   | `/keys/<id>/reactivate`     | secret | Reactivate a key that hit the error threshold  |
| `GET`    | `/nodes`                    | secret | All registered nodes                           |
| `DELETE` | `/nodes/<id>`               | secret | Remove a node                                  |
| `POST`   | `/register`                 | open   | Node registration — returns `node_id` + `token` |
| `POST`   | `/heartbeat`                | token  | Node heartbeat (30s interval)                  |
| `POST`   | `/next-key`                 | token  | Node requests a key from the hub pool          |
| `POST`   | `/report`                   | token  | Node reports key success/failure               |
| `POST`   | `/poll-work`                | token  | Node polls for hub-assigned work (NAT-safe)    |
| `POST`   | `/complete-work`            | token  | Node returns completed work result             |

**Auth header:** `Authorization: Bearer <ZENPOOL_SECRET>`

---

## Security

- **SSRF protection** — node proxy URLs are validated against blocked RFC-1918 / loopback ranges before any outbound connection
- **Key masking** — API keys are never returned in full via the API (`sk-ab...cdef`)
- **Node tokens** — each registered node gets an HMAC token; heartbeat/poll-work/next-key verify it when `ZENPOOL_SECRET` is set
- **Required auth** — set `ZENPOOL_SECRET` to lock down key management endpoints

---

## Persistence & reliability

- SQLite WAL mode — concurrent reads, atomic writes, crash-safe
- Automatic migration from `zenpool-data.json` on first start
- Node state persists across restarts (node ID + token stored locally)
- Public IP refreshed every 15s via `checkip.amazonaws.com`
- Dead nodes pruned after 90s; pending work cancelled immediately

---

## Models

| ID                       | Notes              |
|--------------------------|--------------------|
| `big-pickle`             | Default            |
| `deepseek-v4-flash-free` | Fast, free tier    |
| `deepseek-v4-pro`        | Pro tier           |
| `nemotron-3-super-free`  | Free tier          |
| `mimo-v2.5-free`         | Free tier          |

---

## Requirements

- Python 3.10+ (stdlib only — `sqlite3` is included)
- No pip installs needed

---

🐍🐙∞
