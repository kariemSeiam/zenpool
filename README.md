# 🐍 ZenPool

**Distributed API key pool for OpenCode Zen — never stop.**  
One endpoint. Any number of keys. Zero-config nodes that auto-donate.

```bash
curl -fsSL https://raw.githubusercontent.com/kariemSeiam/zenpool/master/install.sh | bash
```

---

## The Main Thing

```
                         ┌──────────────────────────────┐
                         │   https://srv880434.hstgr.cloud  │
                         │       /v1/chat/completions    │
                         └──────────────┬───────────────┘
                                        │
                         ┌──────────────▼───────────────┐
                         │            HUB               │
                         │  ┌────────────────────────┐  │
                         │  │   Local keys (primary)  │  │
                         │  │   Node keys (fallback)  │  │
                         │  └────────────────────────┘  │
                         └──────────────┬───────────────┘
                                        │
          ┌─────────────────────────────┼─────────────────────────────┐
          ▼                             ▼                             ▼
   ┌──────────────┐            ┌──────────────┐            ┌──────────────┐
   │   Node A     │            │   Node B     │            │   Node C     │
   │  --key sk-1  │            │  --key sk-2  │            │  no key      │
   │   ↓          │            │   ↓          │            │   (borrows)  │
   │ auto-donates │            │ auto-donates │            └──────────────┘
   │ to hub pool  │            │ to hub pool  │
   └──────────────┘            └──────────────┘
```

**You hit the hub → hub grabs the next fresh key from the pool → calls OpenCode → returns.**

- Hub checks **local keys first** (round-robin)
- If all local keys are in cooldown → falls through to **node-contributed keys**
- When a key hits 429 → hub cools it (5m → 10m → 20m → ... → 1h)
- When a node dies → hub removes its key from the pool

---

## Quick Start

### 🚀 One-command (any OS)

```bash
curl -fsSL https://raw.githubusercontent.com/kariemSeiam/zenpool/master/install.sh | bash
```

Auto-detects OS, downloads `zenpool.py`, sets up background service:

| OS | Service | Survives reboot |
|----|---------|-----------------|
| Linux (root) | Systemd system service | ✅ |
| Linux (user) | Systemd user service + linger | ✅ |
| macOS | LaunchAgent | ✅ |
| Windows | Scheduled Task | ✅ |
| Termux | `.bashrc` auto-start | ✅ |

```bash
# Node with key donation (contributes to hub fallback pool)
curl -fsSL ... | bash -s -- --key sk-xxxxx

# Hub server (manages the key pool)
curl -fsSL ... | sudo bash -s -- --hub
```

### Manual

```bash
# Hub
python3 zenpool.py hub

# Node (auto-connects to default hub)
python3 zenpool.py node

# Node with key donation
python3 zenpool.py node --key sk-your-key-here
```

### API usage

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://srv880434.hstgr.cloud/v1",
    api_key="ignored"
)
```

---

## Key Features

| Feature | What it does |
|---------|-------------|
| **Round-robin** | Distributes across all non-cooled keys |
| **Exponential backoff** | 5m → 10m → 20m → 40m → 1h on 429 |
| **Node key donation** | Every `--key` on any device auto-feeds the hub fallback pool |
| **Fallback pool** | When local keys are dry, hub uses node-contributed keys |
| **Auto-pruning** | Dead nodes → their keys leave the pool (90s timeout) |
| **Cross-platform install** | Linux, macOS, Windows, Termux — one command |
| **Background service** | systemd / launchd / scheduled task |
| **Zero dependencies** | Python stdlib only (3.8+) |
| **Concurrent** | ThreadingHTTPServer handles parallel requests |

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Hub status + key/node counts |
| GET | `/keys` | All local keys + cooldown status |
| POST | `/keys` | Add a key to the pool |
| DELETE | `/keys/<id>` | Remove a key |
| POST | `/next-key` | Get next available key (round-robin) |
| POST | `/report` | Report success/failure for a key |
| POST | `/register` | Node registration (auto-sends `--key`) |
| POST | `/heartbeat` | Node heartbeat (30s interval) |
| POST | `/v1/chat/completions` | **Direct proxy — main endpoint** |
| GET | `/v1/models` | Available models |
| GET | `/nodes` | List registered nodes |

---

🐍🐙∞
