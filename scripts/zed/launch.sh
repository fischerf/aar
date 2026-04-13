#!/usr/bin/env bash
# Aar ACP launcher — invoked by Zed when the agent server extension starts.
#
# Zed passes the desired port via ZED_AGENT_PORT; we fall back to 8000.
# The ANTHROPIC_API_KEY (or equivalent) must be set in Zed's environment
# settings or the user's shell profile.
set -euo pipefail

PORT="${ZED_AGENT_PORT:-8000}"
HOST="${ZED_AGENT_HOST:-127.0.0.1}"

# Install aar-agent if not present (first run after extension install).
if ! command -v aar &>/dev/null; then
    echo "[aar-zed] aar command not found — installing aar-agent from PyPI..." >&2
    python3 -m pip install --quiet --user "aar-agent>=0.3.2"
    # Reload PATH so the freshly-installed aar binary is found.
    export PATH="$(python3 -m site --user-base)/bin:${PATH}"
fi

exec aar acp --host "${HOST}" --port "${PORT}"
