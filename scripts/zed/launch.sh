#!/usr/bin/env bash
# Aar ACP launcher — invoked by Zed when the agent server extension starts.
#
# Zed launches this script over stdio.  ZED_AGENT_PORT / ZED_AGENT_HOST are
# only meaningful when `aar acp --http` is in use; in stdio mode (the default
# used by Zed) they are harmless extra flags.
#
# This script assumes `aar` is already on $PATH.  aar-agent is not yet
# published to PyPI, so installation must happen out-of-band — see the error
# message below for the supported install command.
set -euo pipefail

PORT="${ZED_AGENT_PORT:-8000}"
HOST="${ZED_AGENT_HOST:-127.0.0.1}"

if ! command -v aar &>/dev/null; then
    cat >&2 <<'EOF'
[aar-zed] ERROR: 'aar' command not found on PATH.

  aar-agent is not yet published to PyPI, so the Zed launcher cannot
  install it for you.  Please install it from source first:

      pip install --user "git+https://github.com/fischerf/aar.git@v0.4.0#egg=aar-agent[all]"

  or, for a local development checkout:

      git clone https://github.com/fischerf/aar.git
      cd aar
      pip install -e ".[all]"

  After installation, confirm 'aar --help' works in a fresh shell, then
  reload the Zed agent server.

  See https://github.com/fischerf/aar#installation for full instructions.
EOF
    exit 127
fi

exec aar acp --host "${HOST}" --port "${PORT}"
