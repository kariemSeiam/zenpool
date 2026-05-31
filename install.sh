#!/usr/bin/env bash
set -euo pipefail

REPO="https://raw.githubusercontent.com/kariemSeiam/zenpool/master"
DEST="/opt/zenpool"

echo "  🐍 Installing ZenPool Hub..."

# Create directory
mkdir -p "$DEST"

# Download files
curl -fsSL "$REPO/zenpool.py" -o "$DEST/zenpool.py"
chmod +x "$DEST/zenpool.py"

# Install systemd service
curl -fsSL "$REPO/zenpool-hub.service" -o /etc/systemd/system/zenpool-hub.service
chmod 644 /etc/systemd/system/zenpool-hub.service

# Reload systemd, enable and start
systemctl daemon-reload
systemctl enable zenpool-hub
systemctl restart zenpool-hub

echo "  ✅ ZenPool Hub installed at $DEST"
echo "  ├─ Service: zenpool-hub"
echo "  ├─ Port: 5051"
echo "  └─ Logs: journalctl -u zenpool-hub -f"
echo ""
echo "  Add keys: curl -X POST http://localhost:5051/keys \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"key\":\"sk-xxx\",\"label\":\"my-key\"}'"
