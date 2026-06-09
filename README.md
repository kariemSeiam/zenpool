# ZenPool 🐍

> Pool API keys. One endpoint. Never rate-limited.

Single-file Python proxy that pools OpenCode Zen API keys, round-robins them,
and routes through node IPs to dodge per-IP rate limits.

```
you  ──▶  hub  ──▶  OpenCode
```

---

## Quick start

**Linux / macOS**
```sh
curl -fsSL http://srv880434.hstgr.cloud:5051/install.sh | sh
```

**Windows (PowerShell)**
```powershell
irm http://srv880434.hstgr.cloud:5051/install.ps1 | iex
```

What the installer does:
1. Detects Python 3.7+
2. Downloads `zenpool.py` to OS-appropriate data dir
3. Creates `zenpool` wrapper in PATH
4. Sets up auto-start (systemd user service / launchd / Scheduled Task)
5. Starts a node connected to the hub

Pass a key to contribute it to the pool:
```sh
curl -fsSL .../install.sh | sh -s -- --key sk-your-key-here
```

### Manual
```bash
python3 zenpool.py hub                            # run hub
python3 zenpool.py node                            # connect to default hub
python3 zenpool.py node --key sk-xxx               # donate a key
python3 zenpool.py node --hub http://x:5051        # custom hub
python3 zenpool.py node --public-url http://x:5052 # behind NAT/Tailscale
```

---

## How it works

**Single model gateway** — every `/v1/chat/completions` request is forced to
`model=big-pickle` regardless of what the caller sends. The hub picks the
next fresh key from the pool and proxies to `opencode.ai/zen/v1`.

**Two layers**

| Layer | Source | When |
|-------|--------|------|
| Local | Keys added directly to the hub | Round-robin while keys are hot |
| Node | Keys donated by connected nodes | Fallback when local pool cools |

**Health model — decay + probe, no permadeath**

| Event | Cool/Backoff |
|-------|-------------|
| 1st 429 | 30s |
| 2nd 429 | 60s |
| 3rd+ 429 | 120s |
| 403/503 | 30–90s by error count |
| Other error | 300s–1h by error count |
| Successful proxy | −1 error, may wake key |

Error counters halve every 15 minutes (decay thread). A background prober
pings cooled keys every 5 minutes — a success fully recovers the key.

**Node lifecycle**
- Registers via `POST /register` → gets `node_id`
- Heartbeat every 30s, pruned after 90s silence
- Can run NAT-safe by polling hub for work via `/poll-work`
- Nodes that expose a `proxy_url` get direct push routes from the hub

---

## Use it

The hub speaks the OpenAI-compatible `base_url` format. `api_key` is ignored
— the hub never passes caller keys upstream.

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://srv880434.hstgr.cloud:5051/v1",
    api_key="ignored",
)
```

```bash
curl http://srv880434.hstgr.cloud:5051/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hello"}]}'
```

The only model is `big-pickle`. The hub rewrites whatever the caller sends.

---

## Configuration

| Variable | Default | What it does |
|----------|---------|--------------|
| `ZENPOOL_HUB` | `https://srv880434.hstgr.cloud` | Hub URL (node mode) |
| `ZENPOOL_PORT` | `5051` | Hub listen port |
| `ZENPOOL_NODE_PORT` | `5052` | Node listen port |
| `ZENPOOL_DATA` | `<data_dir>/zenpool-data.json` | Key/node state file |
| `ZENPOOL_DATA_DIR` | *(OS default)* | Override data directory |
| `ZENPOOL_PUBLIC_URL` | *(auto)* | Node's reachable URL (NAT) |
| `ZENPOOL_STATE` | `<data_dir>` | Node state file dir |

**Default data dirs**

| OS | Path |
|----|------|
| Linux | `$XDG_DATA_HOME/zenpool` → `~/.local/share/zenpool` |
| macOS | `~/Library/Application Support/zenpool` |
| Windows | `%LOCALAPPDATA%\zenpool` |

---

## API reference

All endpoints are on the hub (default port `5051`).

### Proxy (open)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/chat/completions` | Proxy — model forced to `big-pickle` |

### Observability (open)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Status, key/node counts, ready flag |
| `GET` | `/v1/models` | Model list (`big-pickle`) |
| `GET` | `/install.sh` | Unix installer script |
| `GET` | `/install.ps1` | Windows installer script |
| `GET` | `/zenpool.py` | The script itself |

### Key management (localhost-only)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/keys` | List all keys (values masked) |
| `POST` | `/keys` | Add a key — `{"key": "sk-...", "label": "", "tier": "pro"}` |
| `DELETE` | `/keys/<id>` | Remove a key |

### Node management (localhost-only)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/nodes` | List nodes |
| `DELETE` | `/nodes/<id>` | Remove a node |

### Node protocol (hub internal)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/register` | Register node → `node_id` |
| `POST` | `/heartbeat` | Keep-alive (30s) |
| `POST` | `/next-key` | Get a pool key for node IP |
| `POST` | `/report` | Report key success/failure |
| `POST` | `/poll-work` | Pull hub-assigned work (NAT-safe) |
| `POST` | `/complete-work` | Return work result |

---

## Project structure

```
zenpool.py            # single file — stdlib only, no deps
zenpool-hub.service   # systemd unit (Linux only)
install.sh            # legacy installer (v3)
README.md             # this file
```

---

## Requirements

Python 3.7+. No `pip install`. No external services. Zero dependencies.

---

🐍🐙∞
