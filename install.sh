#!/bin/sh
# ZenPool — distributed key proxy for OpenCode Zen
# Usage: curl -fsSL https://raw.githubusercontent.com/kariemSeiam/zenpool/main/install.sh | sh

set -e

echo "  🐍 ZenPool — installing..."

URL="https://raw.githubusercontent.com/kariemSeiam/zenpool/main/zenpool.py"
DEST="${ZENPOOL_DEST:-/usr/local/bin/zenpool}"

if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$DEST"
elif command -v wget >/dev/null 2>&1; then
    wget -q "$URL" -O "$DEST"
else
    echo "  ❌ Need curl or wget"
    exit 1
fi

chmod +x "$DEST"
echo "  ✅ Installed to $DEST"
echo
echo "  Run:  zenpool hub"
echo "  Or:   zenpool node --hub http://host:5051"
echo "  Or:   zenpool node --key sk-xxxxx"
