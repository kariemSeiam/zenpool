# 🐍 ZenPool

**Distributed API key pool for OpenCode Zen.**  
Pool multiple keys, round-robin across them, auto-cooldown on rate limits. Zero dependencies.

## Architecture

```
                   ┌──────────────┐
                   │   ZenPool    │
                   │    Hub       │
                   │  (key pool)  │
                   │  5+ keys     │
                   └──────┬───────┘
                          │
            ┌─────────────┼──────────────┐
            ▼             ▼              ▼
     ┌──────────┐  ┌──────────┐  ┌──────────────┐
     │  Node A  │  │  Node B  │  │  Production  │
     │ (device) │  │ (device) │  │  App (mcrm)  │
     │ └─asks   │  │ └─has    │  │ └─uses hub's │
     │   hub    │  │   own    │  │   API for    │
     │   for key│  │   --key  │  │   key mgmt   │
     └────┬─────┘  └────┬─────┘  └──────┬───────┘
          │             │               │
          └─────────────┼───────────────┘
                        ▼
               ┌──────────────────┐
               │   OpenCode API   │
               │ opencode.ai/zen  │
               └──────────────────┘
```

**Each request cycle:**

```
1. App/Node → Hub:     "give me a key"     POST /next-key
2. Hub      → App:      "use acc-3"
3. App      → OpenCode:  POST /v1/chat/completions  (with key)
4. App      → Hub:      "success/fail"     POST /report
```

Hub is **not in the request path** — it only manages the key pool.  
If hub dies, nodes with `--key` keep working independently.

## Quick Start

### Hub (key manager)

```bash
python3 zenpool.py hub
curl -X POST http://localhost:5051/keys \
  -H "Content-Type: application/json" \
  -d '{"key": "sk-...", "label": "acc-1"}'
```

### Node (with hub)

```bash
python3 zenpool.py node --hub http://your-server:5051
```

### Node (standalone, no hub needed)

```bash
python3 zenpool.py node --key sk-your-key-here
```

Then use `http://localhost:5052/v1/chat/completions` in any OpenAI-compatible client.

### Production app (direct API)

```python
import urllib.request, json

# 1. Get a key from hub
r = urllib.request.urlopen("http://localhost:5051/next-key")
key = json.loads(r.read())["key"]

# 2. Call OpenCode directly
req = urllib.request.Request(
    "https://opencode.ai/zen/v1/chat/completions",
    data=json.dumps({"model":"deepseek-v4-flash-free","messages":[{"role":"user","content":"hi"}]}).encode(),
    headers={"Content-Type":"application/json","Authorization":f"Bearer {key}","User-Agent":"curl/7.76.1"}
)
r = urllib.request.urlopen(req)

# 3. Report back
urllib.request.urlopen("http://localhost:5051/report",
    data=json.dumps({"key_id": key_id, "ok": True}).encode(),
    headers={"Content-Type":"application/json"})
```

## Key Features

- **Round-robin** across all non-rate-limited keys
- **Exponential backoff** on 429: 5m → 10m → 20m → 40m → 1h max
- **ThreadingHTTPServer** — handles concurrent requests
- **`--key` flag** — run node standalone, no hub dependency
- **Zero dependencies** — stdlib only, Python 3.8+

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Hub status |
| GET | `/keys` | All keys |
| POST | `/keys` | Add key |
| DELETE | `/keys/<id>` | Remove key |
| POST | `/next-key` | Get next available key (RR) |
| POST | `/report` | Report success/error |
| POST | `/register` | Node registration |
| POST | `/heartbeat` | Node heartbeat |
| POST | `/v1/chat/completions` | Direct proxy through hub |
