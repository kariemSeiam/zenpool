#!/bin/bash
# ──────────────────────────────────────────────────────────────────────
#  🐍 ZenPool Installer v2 — Cross-platform, zero-dependency, premium
#  Usage:
#    curl -fsSL https://raw.githubusercontent.com/kariemSeiam/zenpool/master/install.sh | bash
#    curl -fsSL ... | bash -s -- --key sk-xxx     # node with key donation
#    curl -fsSL ... | bash -s -- --hub            # install as hub server
#    curl -fsSL ... | bash -s -- --hub --key sk-xxx
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

# ─── ANSI ────────────────────────────────────────────────────────────
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'
ACCENT='\033[38;2;0;229;204m'
ACCENT2='\033[38;2;255;77;77m'
INFO='\033[38;2;136;146;176m'
SUCCESS='\033[38;2;0;229;204m'
WARN='\033[38;2;255;176;32m'
ERROR='\033[38;2;230;57;70m'
MUTED='\033[38;2;90;100;128m'

# ─── Config ──────────────────────────────────────────────────────────
REPO="https://raw.githubusercontent.com/kariemSeiam/zenpool/master"
DEFAULT_HUB="http://srv880434.hstgr.cloud:5051"
VERSION="2.0.0"
MODE="node"
KEY=""
HUB="$DEFAULT_HUB"
DRY_RUN=0
VERBOSE=0
NO_PROMPT=0
NO_ONBOARD=0
VERIFY=0
GUM_VERSION="0.17.0"
GUM=""
GUM_STATUS="skipped"
GUM_REASON=""

# What we'll install
INSTALL_DIR=""
SERVICE_TYPE="user"
OS=""
INIT=""
TMPFILES=()
TAGLINE=""

cleanup() {
    local f
    for f in "${TMPFILES[@]:-}"; do
        rm -rf "$f" 2>/dev/null || true
    done
}
trap cleanup EXIT

mktempfile() {
    local f; f="$(mktemp)"; TMPFILES+=("$f"); echo "$f"
}

# ─── UI helpers ──────────────────────────────────────────────────────
is_tty() {
    [[ -t 1 ]] || [[ -n "${GUM}" ]]
}

is_interactive() {
    [[ "$NO_PROMPT" == "1" ]] && return 1
    [[ -t 0 && -t 1 ]] && return 0
    return 1
}

info()  { echo -e "${MUTED}·${NC} $*"; }
warn()  { echo -e "${WARN}!${NC} $*"; }
success() { echo -e "${SUCCESS}✓${NC} $*"; }
error() { echo -e "${ERROR}✗${NC} $*" >&2; }
kv()    { echo -e "${MUTED}$1:${NC} $2"; }
stage() { echo ""; echo -e "${ACCENT}${BOLD}▶ [$1/$2] $3${NC}"; }
header(){ echo -e "${ACCENT}${BOLD}  $1${NC}"; }

# ─── Gum bootstrap (beautiful TUI) ───────────────────────────────────
gum_detect_os() {
    case "$(uname -s 2>/dev/null)" in Darwin) echo "Darwin" ;; Linux) echo "Linux" ;; *) echo "unsupported" ;; esac
}
gum_detect_arch() {
    case "$(uname -m 2>/dev/null)" in
        x86_64|amd64) echo "x86_64" ;;
        arm64|aarch64) echo "arm64" ;;
        *) echo "unknown" ;;
    esac
}
gum_bootstrap() {
    GUM=""; GUM_STATUS="skipped"
    ! is_tty && { GUM_REASON="not a tty"; return 1; }
    command -v gum &>/dev/null && { GUM="gum"; GUM_STATUS="found"; return 0; }
    local os arch asset url tmpdir gum_path
    os="$(gum_detect_os)"; arch="$(gum_detect_arch)"
    [[ "$os" == "unsupported" || "$arch" == "unknown" ]] && { GUM_REASON="unsupported os/arch"; return 1; }
    asset="gum_${GUM_VERSION}_${os}_${arch}.tar.gz"
    url="https://github.com/charmbracelet/gum/releases/download/v${GUM_VERSION}/${asset}"
    tmpdir="$(mktemp -d)"; TMPFILES+=("$tmpdir")
    info "Loading spinner support..."
    curl -fsSL --retry 3 --retry-delay 1 -o "$tmpdir/$asset" "$url" 2>/dev/null || return 1
    tar -xzf "$tmpdir/$asset" -C "$tmpdir" 2>/dev/null || return 1
    gum_path="$(find "$tmpdir" -type f -name gum 2>/dev/null | head -1)" || return 1
    chmod +x "$gum_path"
    GUM="$gum_path"; GUM_STATUS="installed"; return 0
}

gum_style() { [[ -n "$GUM" ]] && "$GUM" style "$@" || echo "$2"; }

gum_spin() {
    local title="$1"; shift
    local -a cmd=("$@")
    local is_func; is_func=false
    declare -F "${cmd[0]}" &>/dev/null && is_func=true

    if [[ -n "$GUM" ]] && ! $is_func; then
        "$GUM" spin --spinner dot --title "$title" -- "${cmd[@]}" || {
            GUM=""; GUM_STATUS="skipped"
            info "$title"
            "${cmd[@]}"
            return $?
        }
    else
        info "$title"
        "${cmd[@]}"
    fi
}

ui_plan() {
    local content="$1"
    if [[ -n "$GUM" ]]; then
        local styled; styled="$("$GUM" style --foreground "#8892b0" "$content")"
        "$GUM" style --border rounded --border-foreground "#5a6480" --padding "0 1" "$styled"
    else
        echo "$content"
    fi
}

# ─── OS detection ────────────────────────────────────────────────────
detect_os() {
    case "$(uname -s)" in
        Linux*)  OS="linux" ;;
        Darwin*) OS="macos" ;;
        CYGWIN*|MINGW*|MSYS*) OS="windows" ;;
        *)       OS="linux" ;;
    esac
    [[ "$(id -u)" == "0" ]] && SERVICE_TYPE="system" || SERVICE_TYPE="user"
    command -v systemctl &>/dev/null && INIT="systemd" && return
    command -v launchctl &>/dev/null && INIT="launchd" && return
    INIT="generic"
}

# ─── Paths ───────────────────────────────────────────────────────────
set_paths() {
    case "$OS" in
        linux)
            if [[ "$SERVICE_TYPE" == "system" ]]; then
                INSTALL_DIR="/opt/zenpool"
            else
                INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/zenpool"
            fi
            ;;
        macos)   INSTALL_DIR="$HOME/Library/Application Support/zenpool" ;;
        windows) INSTALL_DIR="${USERPROFILE:-$HOME}/zenpool" ;;
    esac
    mkdir -p "$INSTALL_DIR"
}

# ─── Download ────────────────────────────────────────────────────────
download_script() {
    local dest="$1"
    if command -v curl &>/dev/null; then
        curl -fsSL --retry 3 --retry-delay 2 "$REPO/zenpool.py" -o "$dest"
    elif command -v wget &>/dev/null; then
        wget -q --tries=3 "$REPO/zenpool.py" -O "$dest"
    else
        error "Need curl or wget"; exit 1
    fi
    chmod +x "$dest"
}

# ─── Service installers ──────────────────────────────────────────────
install_systemd_node() {
    local svc_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$svc_dir"
    local exec_start="python3 $INSTALL_DIR/zenpool.py node --hub $HUB"
    [[ -n "$KEY" ]] && exec_start+=" --key $KEY"

    cat > "$svc_dir/zenpool-node.service" << EOSERVICE
[Unit]
Description=ZenPool Node — key donor for OpenCode
Documentation=https://github.com/kariemSeiam/zenpool
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$exec_start
WorkingDirectory=$INSTALL_DIR
Restart=on-failure
RestartSec=10
NoNewPrivileges=true

[Install]
WantedBy=default.target
EOSERVICE
    systemctl --user daemon-reload
    systemctl --user enable --now zenpool-node.service

    # Linger so service survives logout
    if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
        info "Enabling linger for $USER (survives logout)..."
        # Try without sudo first, fall back
        loginctl enable-linger "$USER" 2>/dev/null || \
            sudo loginctl enable-linger "$USER" 2>/dev/null || \
            warn "Could not enable linger — service runs only while logged in"
    fi
    success "Node service installed (zenpool-node)"
}

install_systemd_hub() {
    local svc_file="/etc/systemd/system/zenpool-hub.service"
    if [[ "$SERVICE_TYPE" == "user" ]]; then
        warn "Hub requires system-level service (needs sudo for /etc)"
        if ! is_interactive; then
            error "Cannot install hub in non-interactive mode without root. Use: curl -fsSL ... | sudo bash -s -- --hub"
            exit 1
        fi
        echo -e "${WARN}Re-run with sudo to install hub as system service${NC}"
        echo "  curl -fsSL $REPO/install.sh | sudo bash -s -- --hub"
        exit 1
    fi

    cat > "$svc_file" << 'EOSERVICE'
[Unit]
Description=ZenPool Hub — distributed key proxy for OpenCode
Documentation=https://github.com/kariemSeiam/zenpool
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/zenpool/zenpool.py hub
WorkingDirectory=/opt/zenpool
Restart=always
RestartSec=5
Environment=ZENPOOL_PORT=5051
Environment=ZENPOOL_DATA=/opt/zenpool/zenpool-data.json

[Install]
WantedBy=multi-user.target
EOSERVICE
    systemctl daemon-reload
    systemctl enable --now zenpool-hub
    success "Hub service installed (zenpool-hub)"
}

install_launchd_node() {
    local label="com.zenpool.node"
    local plist="$HOME/Library/LaunchAgents/${label}.plist"
    mkdir -p "$(dirname "$plist")"

    local key_args=""
    [[ -n "$KEY" ]] && key_args="<string>--key</string>
    <string>$KEY</string>"

    cat > "$plist" << EOPLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$INSTALL_DIR/zenpool.py</string>
    <string>node</string>
    <string>--hub</string>
    <string>$HUB</string>
    $key_args
  </array>
  <key>WorkingDirectory</key>
  <string>$INSTALL_DIR</string>
  <key>KeepAlive</key>
  <true/>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$INSTALL_DIR/zenpool.log</string>
  <key>StandardErrorPath</key>
  <string>$INSTALL_DIR/zenpool.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/bin:/usr/local/bin:/opt/homebrew/bin</string>
  </dict>
</dict>
</plist>
EOPLIST
    launchctl load "$plist"
    success "LaunchAgent installed ($label)"
}

install_launchd_hub() {
    warn "Hub on macOS: install as system daemon or run manually:"
    echo "  python3 $INSTALL_DIR/zenpool.py hub"
    echo ""
}

install_windows_node() {
    local ps1="$INSTALL_DIR/run-zenpool.ps1"
    cat > "$ps1" << EOPS
# ZenPool Node runner
cd "$INSTALL_DIR"
python3 zenpool.py node --hub $HUB ${KEY:+--key $KEY}
EOPS

    local task_name="ZenPoolNode"
    powershell.exe -Command "
      `$action = New-ScheduledTaskAction -Execute 'python3' -Argument '$(tail -1 "$ps1")'
      `$trigger = New-ScheduledTaskTrigger -AtStartup
      `$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
      Register-ScheduledTask -TaskName '$task_name' -Action `$action -Trigger `$trigger -Settings `$settings -Force
      Start-ScheduledTask -TaskName '$task_name'
    " 2>/dev/null && success "Scheduled task created ($task_name)" || {
        # Fallback: startup folder
        local startup="$USERPROFILE/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
        [[ -d "$startup" ]] && cat > "$startup/zenpool-node.cmd" << EOCMD
@echo off
start /b python3 "$INSTALL_DIR/zenpool.py" node --hub $HUB ${KEY:+--key $KEY}
EOCMD
        success "Startup entry added"
    }
}

install_windows_hub() {
    warn "Hub on Windows: install via WSL/Linux or run manually:"
    echo "  cd $INSTALL_DIR && python3 zenpool.py hub"
}

install_generic_bg() {
    local rc_file="${ZDOTDIR:-$HOME}/.zshrc"
    [[ -f "$rc_file" ]] || rc_file="$HOME/.bashrc"
    [[ -f "$rc_file" ]] || rc_file="$HOME/.profile"

    local cmd="nohup python3 $INSTALL_DIR/zenpool.py node --hub $HUB ${KEY:+--key $KEY} > $INSTALL_DIR/zenpool.log 2>&1 &"
    if ! grep -q "zenpool" "$rc_file" 2>/dev/null; then
        cat >> "$rc_file" << EORC

# 🐍 ZenPool node — background
if ! pgrep -f "zenpool.py.*node" >/dev/null 2>&1; then
  $cmd
fi
EORC
        success "Added to $rc_file (background on shell start)"
    fi
}

# ─── Taglines ────────────────────────────────────────────────────────
TAGLINES=(
    "Your keys just got a raise — they're pooling now."
    "One endpoint to rule them all."
    "Rate limits? Never heard of her."
    "Every node is a lifeline. Every key is a lane."
    "The hub never sleeps. Neither does your API access."
    "Distributed by design. Resilient by default."
    "Install once. Pool forever."
    "Your OpenCode keys, working in shifts."
    "6 keys, 1 endpoint, ∞ uptime."
    "Like a toll booth, but the lanes keep multiplying."
    "Auto-failover. Auto-cooldown. Auto-you-don't-think-about-it."
    "Zero-dependency. Zero-config. Zero-stress."
    "When the 429 hits, the pool adapts."
    "Your laptop's keys are now serverless."
    "The mesh grows stronger with every node."
    "Turn any device into a key donor."
    "Python stdlib only. No npm. No drama."
    "Exponential backoff, linear peace of mind."
    "Hub → Nodes → ∞ keys → never stop."
    "If it runs Python, it can run ZenPool."
)

pick_tagline() {
    local idx=$((RANDOM % ${#TAGLINES[@]}))
    echo "${TAGLINES[$idx]}"
}

# ─── Post-install ────────────────────────────────────────────────────
show_output() {
    echo ""
    if [[ -n "$GUM" ]]; then
        local hi msg1 msg2 msg3
        hi="$("$GUM" style --foreground "#00e5cc" --bold "  🐍 ZenPool $MODE installed")"
        msg1="$("$GUM" style --foreground "#8892b0" "  $TAGLINE")"
        local lines; lines=$(printf '%s\n%s' "$hi" "$msg1")
        "$GUM" style --border rounded --border-foreground "#00e5cc" --padding "1 2" "$lines"
    else
        echo -e "${ACCENT}${BOLD}  🐍 ZenPool $MODE installed${NC}"
        echo -e "${INFO}  $TAGLINE${NC}"
    fi
    echo ""
    if [[ "$MODE" == "hub" ]]; then
        echo -e "  ${BOLD}Main endpoint:${NC}  http://0.0.0.0:5051/v1/chat/completions"
        echo ""
        echo "  Add a key:"
        echo "    curl -X POST http://localhost:5051/keys \\"
        echo "      -H 'Content-Type: application/json' \\"
        echo "      -d '{\"key\":\"sk-your-key\",\"label\":\"my-key\"}'"
    else
        echo -e "  ${BOLD}Main endpoint:${NC}  $HUB/v1/chat/completions"
        echo -e "  ${BOLD}Node ID:${NC}       see \`curl -s http://localhost:5052/health\`"
        [[ -n "$KEY" ]] && echo -e "  ${BOLD}Key donated:${NC}   ✓ (hub will use as fallback)"
        echo ""
        echo "  This node auto-registered with the hub and heartbeats every 30s."
        echo "  If the hub's local keys are exhausted, it will route through yours."
    fi
    echo ""
    echo -e "  ${BOLD}Logs:${NC}"
    case "$OS" in
        linux)  echo "    journalctl --user -u zenpool-node -f" ;;
        macos)  echo "    tail -f '$INSTALL_DIR/zenpool.log'" ;;
        windows) echo "    type '$INSTALL_DIR/zenpool.log'" ;;
    esac
    echo ""
}

verify_connectivity() {
    info "Verifying hub connectivity..."
    sleep 2
    local h; h="$(curl -sf "$HUB/health" 2>/dev/null)" && {
        local keys; keys="$(echo "$h" | python3 -c "import sys,json; print(json.load(sys.stdin).get('keys','?'))" 2>/dev/null || echo "?")"
        success "Hub reachable ($keys keys in pool)"
    } || warn "Hub not reachable yet — check when online"
}

# ─── Main ────────────────────────────────────────────────────────────
main() {
    TAGLINE="$(pick_tagline)"
    detect_os
    set_paths
    gum_bootstrap 2>/dev/null || true

    # ── Stage 1: Plan ──
    stage 1 3 "Planning"

    local plan="OS: $OS\nInit: $INIT\nMode: $MODE\nDir: $INSTALL_DIR"
    [[ "$MODE" == "node" ]] && plan+="\nHub: $HUB"
    [[ -n "$KEY" ]] && plan+="\nKey: ✓ will donate to hub"
    ui_plan "$(echo -e "$plan")"

    # ── Stage 2: Install ──
    stage 2 3 "Installing"

    gum_spin "Downloading ZenPool v$VERSION..." \
        download_script "$INSTALL_DIR/zenpool.py"

    # Detect existing service
    local existing=""
    if systemctl --user list-unit-files 2>/dev/null | grep -q "zenpool-node"; then
        existing="zenpool-node"
    elif systemctl list-unit-files 2>/dev/null | grep -q "zenpool-hub"; then
        existing="zenpool-hub"
    fi
    [[ -n "$existing" ]] && info "Upgrading existing $existing..."

    case "$OS" in
        linux)
            if [[ "$MODE" == "hub" ]]; then
                [[ "$SERVICE_TYPE" != "system" ]] && { error "Hub needs root: sudo bash ... -- --hub"; exit 1; }
                gum_spin "Installing hub service" install_systemd_hub
            else
                gum_spin "Installing node service" install_systemd_node
            fi
            ;;
        macos)
            if [[ "$MODE" == "hub" ]]; then
                install_launchd_hub
            else
                gum_spin "Installing launch agent" install_launchd_node
            fi
            ;;
        windows)
            if [[ "$MODE" == "hub" ]]; then
                install_windows_hub
            else
                gum_spin "Installing background task" install_windows_node
            fi
            ;;
        *)
            gum_spin "Setting up background runner" install_generic_bg
            ;;
    esac

    # ── Stage 3: Verify ──
    stage 3 3 "Verifying"
    if [[ "$MODE" == "node" ]] && [[ "$OS" != "windows" ]]; then
        sleep 2
        local health
        health="$(curl -sf http://localhost:5052/health 2>/dev/null || true)"
        if [[ -n "$health" ]]; then
            local nid; nid="$(echo "$health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('node','?'))" 2>/dev/null || echo "?")"
            success "Node running (ID: $nid)"
        else
            warn "Node not responding yet — check service status"
        fi
    fi

    [[ "$VERIFY" == "1" ]] && verify_connectivity

    show_output
}

# ─── Parse args ──────────────────────────────────────────────────────
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --hub) MODE="hub" ;;
            --key) shift; KEY="$1" ;;
            --key=*) KEY="${1#*=}" ;;
            --hub=*) HUB="${1#*=}" ;;
            --dry-run) DRY_RUN=1 ;;
            --verbose|--debug) VERBOSE=1; set -x ;;
            --no-prompt) NO_PROMPT=1 ;;
            --no-onboard) NO_ONBOARD=1 ;;
            --verify) VERIFY=1 ;;
            --help|-h)
                echo "ZenPool Installer v$VERSION"
                echo "Usage: curl -fsSL $REPO/install.sh | bash -s -- [options]"
                echo ""
                echo "Options:"
                echo "  --hub              Install as hub server (default: node)"
                echo "  --key <sk-xxx>     API key to donate to hub pool"
                echo "  --hub=<url>        Custom hub URL (default: $DEFAULT_HUB)"
                echo "  --verify           Run connectivity check after install"
                echo "  --dry-run          Print plan, no changes"
                echo "  --verbose          Debug output"
                echo "  --no-prompt        Non-interactive mode"
                echo "  --help             This help"
                exit 0
                ;;
            *) error "Unknown: $1"; exit 1 ;;
        esac
        shift
    done
}

parse_args "$@"
[[ "$DRY_RUN" == "1" ]] && { echo "  DRY RUN — no changes made"; exit 0; }
main
