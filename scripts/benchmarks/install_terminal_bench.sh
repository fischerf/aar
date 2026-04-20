#!/usr/bin/env bash
# install_terminal_bench.sh — one-shot setup for Terminal-Bench 2.0 + Harbor + aar in WSL
#
# Usage (run from the aar repo root inside WSL):
#   bash scripts/benchmarks/install_terminal_bench.sh
#
# What it does:
#   1. Verifies WSL + Docker engine
#   2. Installs Docker Compose plugin (required by Harbor)
#   3. Installs Python 3.11+ (via deadsnakes PPA if needed)
#   4. Installs uv (fast package manager / tool runner)
#   5. Installs Harbor via uv tool
#   6. Installs aar + all provider extras in editable mode
#   7. Creates a sample job config (jobs/terminal_bench_aar.yaml)
#   8. Prints a quick-start cheat sheet

set -euo pipefail

# ── colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}▶${RESET} $*"; }
success() { echo -e "${GREEN}✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}⚠${RESET}  $*"; }
error()   { echo -e "${RED}✗${RESET} $*" >&2; exit 1; }
header()  { echo -e "\n${BOLD}━━━ $* ━━━${RESET}"; }

# ── 0. locate repo root ───────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
info "Repo root: $REPO_ROOT"

# ── 1. verify WSL ─────────────────────────────────────────────────────────────
header "0/8  Environment check"

if ! grep -qi microsoft /proc/version 2>/dev/null; then
    warn "Not running inside WSL. This script targets WSL2."
    read -rp "Continue anyway? [y/N] " yn
    [[ "$yn" =~ ^[Yy]$ ]] || error "Aborted."
fi
success "WSL detected"

# ── 2. Docker ─────────────────────────────────────────────────────────────────
header "1/8  Docker engine"

install_docker_engine() {
    info "Installing Docker Engine (official script)…"
    if ! command -v apt-get &>/dev/null; then
        error "Auto-install only supported on apt-based distros.\n  Install Docker manually: https://docs.docker.com/engine/install/"
    fi

    # Remove any conflicting packages
    for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
        sudo apt-get remove -y "$pkg" 2>/dev/null || true
    done

    sudo apt-get update -qq
    sudo apt-get install -y ca-certificates curl gnupg lsb-release

    # Add Docker's official GPG key + apt repo
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

    sudo apt-get update -qq
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    # Allow current user to run docker without sudo
    sudo usermod -aG docker "$USER"

    # Start daemon (systemd may not be available in all WSL setups)
    if command -v systemctl &>/dev/null && systemctl is-system-running &>/dev/null 2>&1; then
        sudo systemctl enable --now docker
    else
        sudo service docker start || true
    fi

    success "Docker Engine installed — you may need to log out and back in for group changes to take effect"
    warn "If 'docker' commands fail with permission errors, run: newgrp docker"
}

if ! command -v docker &>/dev/null; then
    warn "docker not found — installing Docker Engine…"
    install_docker_engine
fi

# Give the daemon a moment if we just started it
if ! docker info &>/dev/null 2>&1; then
    warn "Docker daemon not yet reachable — attempting to start…"
    sudo service docker start 2>/dev/null || sudo systemctl start docker 2>/dev/null || true
    sleep 3
    if ! docker info &>/dev/null 2>&1; then
        error "Docker daemon is still not reachable.\n  Try: sudo service docker start\n  Then re-run this script."
    fi
fi

DOCKER_VERSION=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "unknown")
success "Docker is running  (v${DOCKER_VERSION})"

header "2/8  Docker Compose plugin"
# Harbor requires `docker compose` (plugin form). Check and install if missing.
if docker compose version &>/dev/null 2>&1; then
    success "docker compose available  ($(docker compose version --short 2>/dev/null || echo 'version unknown'))"
else
    warn "docker compose plugin not found — installing…"
    if command -v apt-get &>/dev/null; then
        # Try the official Docker repo package first; fall back to the Ubuntu universe package.
        if apt-cache show docker-compose-plugin &>/dev/null 2>&1; then
            sudo apt-get install -y docker-compose-plugin
        elif apt-cache show docker-compose-v2 &>/dev/null 2>&1; then
            sudo apt-get install -y docker-compose-v2
        else
            error "Cannot find docker-compose-plugin or docker-compose-v2 in apt.\n  Install Docker from the official repo: https://docs.docker.com/engine/install/"
        fi
    elif command -v apk &>/dev/null; then
        sudo apk add --no-cache docker-cli-compose
    else
        error "Cannot install docker compose automatically.\n  Please install it manually: https://docs.docker.com/compose/install/"
    fi

    if docker compose version &>/dev/null 2>&1; then
        success "docker compose installed  ($(docker compose version --short 2>/dev/null || echo 'version unknown'))"
    else
        error "docker compose still not working after install attempt."
    fi
fi

# ── 3. Python 3.11+ ───────────────────────────────────────────────────────────
header "3/8  Python 3.11+"

pick_python() {
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" &>/dev/null; then
            local ver
            ver=$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
            local major minor
            major="${ver%%.*}"; minor="${ver#*.}"
            if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
                echo "$candidate"; return 0
            fi
        fi
    done
    return 1
}

if PYTHON=$(pick_python); then
    success "Using $PYTHON  ($($PYTHON --version))"
else
    warn "No Python 3.11+ found — installing via deadsnakes PPA"
    if command -v apt-get &>/dev/null; then
        sudo apt-get update -qq
        sudo apt-get install -y software-properties-common
        sudo add-apt-repository -y ppa:deadsnakes/ppa
        sudo apt-get update -qq
        sudo apt-get install -y python3.11 python3.11-venv python3.11-distutils
        PYTHON=python3.11
        success "Installed Python 3.11"
    elif command -v apk &>/dev/null; then
        sudo apk add --no-cache python3 py3-pip
        PYTHON=python3
        success "Installed Python 3 via apk"
    else
        error "Cannot install Python automatically.\n  Please install Python 3.11+ and re-run this script."
    fi
fi

# ── 4. uv ─────────────────────────────────────────────────────────────────────
header "4/8  uv (package manager)"

if command -v uv &>/dev/null; then
    success "uv already installed  ($(uv --version))"
else
    info "Installing uv via the official installer…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # source uv's own env file if present (sets PATH correctly in the current shell)
    # shellcheck disable=SC1091
    [[ -f "$HOME/.local/bin/env" ]]  && source "$HOME/.local/bin/env"
    [[ -f "$HOME/.cargo/env" ]]      && source "$HOME/.cargo/env"
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if ! command -v uv &>/dev/null; then
        error "uv installation failed. Check https://docs.astral.sh/uv/getting-started/installation/"
    fi
    success "uv installed  ($(uv --version))"
fi

# Ensure ~/.local/bin is permanently on PATH (needed for both uv and harbor)
PATH_LINE='export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"'
if ! grep -qF "$PATH_LINE" "$HOME/.bashrc" 2>/dev/null; then
    echo "" >> "$HOME/.bashrc"
    echo "# uv / harbor tools (added by install_terminal_bench.sh)" >> "$HOME/.bashrc"
    echo "$PATH_LINE" >> "$HOME/.bashrc"
    info "Added ~/.local/bin to PATH in ~/.bashrc"
fi
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# ── 5. Harbor ─────────────────────────────────────────────────────────────────
header "5/8  Harbor"

if command -v harbor &>/dev/null; then
    success "Harbor already installed  ($(harbor --version 2>/dev/null || echo 'version unknown'))"
else
    info "Installing Harbor via uv tool…"
    uv tool install harbor
    # uv tools land in ~/.local/bin
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v harbor &>/dev/null; then
        warn "harbor not in PATH after uv tool install."
        warn "Run: export PATH=\"\$HOME/.local/bin:\$PATH\"  then re-run this script, or:"
        warn "     pip install harbor"
        read -rp "Try pip install harbor as fallback? [Y/n] " yn
        [[ "$yn" =~ ^[Nn]$ ]] || pip install harbor
    fi
    success "Harbor installed  ($(harbor --version 2>/dev/null || echo 'version unknown'))"
fi

# ── 6. aar ────────────────────────────────────────────────────────────────────
header "6/8  aar (editable install with all providers)"

cd "$REPO_ROOT"
VENV_DIR="$REPO_ROOT/.venv"

# Create venv if it doesn't exist yet
if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating virtual environment at $VENV_DIR…"
    if command -v uv &>/dev/null; then
        uv venv --python "$PYTHON" "$VENV_DIR"
    else
        "$PYTHON" -m venv "$VENV_DIR"
    fi
fi

# Activate so 'aar' lands on PATH for the rest of this script
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# Install aar into the venv
if command -v uv &>/dev/null; then
    info "Installing aar with uv pip…"
    uv pip install -e ".[all,dev]"
else
    info "Installing aar with pip…"
    pip install -e ".[all,dev]"
fi

AAR_VERSION=$(aar --version 2>/dev/null || echo "unknown")
success "aar installed  ($AAR_VERSION)"

# Persist venv activation for interactive shells
ACTIVATE_LINE="source \"$VENV_DIR/bin/activate\""
if ! grep -qF "$ACTIVATE_LINE" "$HOME/.bashrc" 2>/dev/null; then
    echo "" >> "$HOME/.bashrc"
    echo "# aar venv (added by install_terminal_bench.sh)" >> "$HOME/.bashrc"
    echo "$ACTIVATE_LINE" >> "$HOME/.bashrc"
fi
info "Venv activate line added to ~/.bashrc"
warn "To use aar in this shell now, run:"
warn "  source $VENV_DIR/bin/activate"

# ── aar config setup ──────────────────────────────────────────────────────────
mkdir -p "$HOME/.aar"

# Copy default config_terminalbench20.json (won't overwrite if already customised)
if [[ ! -f "$HOME/.aar/config.json" ]]; then
    cp "$REPO_ROOT/config/samples/config_terminalbench20.json" "$HOME/.aar/config.json"
    success "Copied config/samples/config_terminalbench20.json → ~/.aar/config.json"
else
    success "~/.aar/config.json already exists — skipping"
fi

# Copy rules.md (won't overwrite if already customised)
if [[ ! -f "$HOME/.aar/rules.md" ]]; then
    cp "$REPO_ROOT/config/rules/rules.md" "$HOME/.aar/rules.md"
    success "Copied config/rules/rules.md → ~/.aar/rules.md"
else
    success "~/.aar/rules.md already exists — skipping"
fi

# ── 7. sample job config ──────────────────────────────────────────────────────
header "7/8  Sample job config"

JOBS_DIR="$REPO_ROOT/jobs"
mkdir -p "$JOBS_DIR"
JOB_FILE="$JOBS_DIR/terminal_bench_aar.yaml"

if [[ -f "$JOB_FILE" ]]; then
    success "Job config already exists: $JOB_FILE"
else
    cat > "$JOB_FILE" <<'EOF'
# Repeatable Terminal-Bench 2.0 run config.
# Usage:  harbor run -c jobs/terminal_bench_aar.yaml
#
# Swap import_path to switch between installed vs. external mode:
#   installed (recommended for evals):
#     scripts.benchmarks.terminal_bench.harbor_agent_installed:AarInstalledAgent
#   external (fast iteration, uses local aar):
#     scripts.benchmarks.terminal_bench.harbor_agent_external:AarExternalAgent

datasets:
  - name: terminal-bench
    version: "2.0"

agents:
  - import_path: scripts.benchmarks.terminal_bench.harbor_agent_installed:AarInstalledAgent
    model_name: ollama/gemma4:e4b   # change model here

n_concurrent_trials: 1
EOF
    success "Created $JOB_FILE"
fi

# ── detect Windows host IP for Ollama ────────────────────────────────────────
# Ollama runs on the Windows host. From WSL2 (NAT mode) the Windows host is
# reachable via the default gateway. Docker containers (installed agent) can
# also reach it through the WSL2 host routing.
WINDOWS_HOST_IP=$(ip route show default 2>/dev/null | awk '/default/ {print $3; exit}')
OLLAMA_URL="http://${WINDOWS_HOST_IP:-<windows-host-ip>}:11434"

header "8/8  Quick-start cheat sheet"
echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}  Quick-start (run these in every new shell)${RESET}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
echo -e "  ${YELLOW}0. Shell preamble (harbor lives in ~/.local/bin):${RESET}"
echo "     source $VENV_DIR/bin/activate"
echo "     export PATH=\"\$HOME/.local/bin:\$PATH\""
echo "     cd $REPO_ROOT"
echo ""
echo -e "  ${YELLOW}1. Set your API key / provider URL:${RESET}"
echo "     export ANTHROPIC_API_KEY=sk-ant-..."
echo "     # or: export OPENAI_API_KEY=sk-..."
echo ""
echo -e "  ${YELLOW}   Ollama (Windows host):${RESET}"
echo "     # 1. On Windows: set OLLAMA_HOST=0.0.0.0 and restart Ollama"
echo "     #    (so it listens on all interfaces, not just 127.0.0.1)"
echo "     # 2. Then:"
echo "     export OLLAMA_BASE_URL=${OLLAMA_URL}"
echo "     #    Windows host IP detected above — use this for both WSL and Docker containers."
echo "     #    Do NOT use 'localhost' (means the WSL VM) or 'host.docker.internal'"
echo "     #    (only works with Docker Desktop, not standalone Docker Engine)."
echo ""
echo -e "  ${YELLOW}2a. Via job config (recommended):${RESET}"
echo "     harbor run -c jobs/terminal_bench_aar.yaml"
echo ""
echo -e "  ${YELLOW}2b. Installed agent (one-liner):${RESET}"
echo "     harbor run \\"
echo "       --dataset terminal-bench@2.0 \\"
echo "       --agent-import-path scripts.benchmarks.terminal_bench.harbor_agent_installed:AarInstalledAgent \\"
echo "       --model ollama/gemma4:e4b"
echo ""
echo -e "  ${YELLOW}2c. External agent (fast iteration, uses local aar):${RESET}"
echo "     harbor run \\"
echo "       --dataset terminal-bench@2.0 \\"
echo "       --agent-import-path scripts.benchmarks.terminal_bench.harbor_agent_external:AarExternalAgent \\"
echo "       --model ollama/gemma4:e4b"
echo ""
echo -e "  ${YELLOW}3. View results:${RESET}"
echo "     harbor view jobs              # browse all job results (TUI)"
echo "     harbor trials list"
echo "     harbor trials view <id>"
echo ""
echo -e "  ${YELLOW}Useful flags:${RESET}"
echo "     --n-concurrent 4   # parallel tasks (needs more API quota)"
echo "     --task <id>        # run a single specific task"
echo ""
echo -e "${GREEN}Setup complete.${RESET}"
echo ""
echo -e "${BOLD}${YELLOW}  ⚠  PATH not yet active in this shell. Run now:${RESET}"
echo -e "     ${BOLD}export PATH=\"\$HOME/.local/bin:\$PATH\"${RESET}"
echo -e "     ${BOLD}source $VENV_DIR/bin/activate${RESET}"
