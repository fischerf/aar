# Terminal-Bench 2.0 with Aar

[Terminal-Bench 2.0](https://www.harborframework.com) is run via the
[Harbor Framework](https://www.harborframework.com/docs/getting-started).
This directory provides two agent adapters for evaluating aar on it.

---

## Prerequisites

### 1. Aar sandbox already set up

This guide assumes you have already run `aar sandbox setup` and have a working
WSL2 distro (e.g. `aar-ubuntu`). Run everything below **inside that distro**.

```bash
wsl -d aar-ubuntu
source /mnt/b/Github_my/aar/.venv/bin/activate
cd /mnt/b/Github_my/aar
```

Or in one shot from Windows:

```bash
wsl -d aar-ubuntu -- bash -c "source /mnt/b/Github_my/aar/.venv/bin/activate && cd /mnt/b/Github_my/aar && <command>"
```

### 2. Docker with Compose plugin

Harbor uses `docker compose` (plugin form, not the legacy `docker-compose` binary).
Check:

```bash
docker compose version
```

If missing:

```bash
sudo apt-get install -y docker-compose-v2
```

Make sure the Docker daemon is running:

```bash
sudo service docker start
docker info   # should not error
```

### 3. Harbor

```bash
uv tool install harbor
export PATH="$HOME/.local/bin:$PATH"   # if harbor is not yet on PATH
harbor --version
```

### 4. API key

```bash
export ANTHROPIC_API_KEY=sk-ant-...
# or
export OPENAI_API_KEY=sk-...
# or for Ollama (installed agent only):
export OLLAMA_BASE_URL=http://host.docker.internal:11434
```

---

## Quick start

Everything above is already handled by the install script. From inside the
aar-ubuntu WSL distro:

```bash
source /mnt/b/Github_my/aar/.venv/bin/activate
export PATH="$HOME/.local/bin:$PATH"
export ANTHROPIC_API_KEY=sk-ant-...
cd /mnt/b/Github_my/aar

harbor run -c jobs/terminal_bench_aar.yaml
```

The job config at `jobs/terminal_bench_aar.yaml` is ready to use. Edit
`model_name` there to switch models.

---

## Two integration approaches

| | `harbor_agent_installed.py` | `harbor_agent_external.py` |
|---|---|---|
| **Where aar runs** | Inside the container | On the host (WSL) |
| **File I/O** | Container filesystem ✓ | Container (proxied via `exec`) ✓ |
| **Shell commands** | Container ✓ | Container (via `environment.exec`) ✓ |
| **LLM calls** | Container → API | Host → API |
| **Recommended for** | Leaderboard / accurate evals | Quick iteration / local dev |

---

## Approach A — Installed agent (recommended)

Aar is built from the local repo source, uploaded as a wheel into each Harbor
container, and installed there. All file operations and shell commands run on
the task filesystem inside the container.

### One-off run

```bash
harbor run \
  --dataset terminal-bench@2.0 \
  --agent-import-path scripts.benchmarks.terminal_bench.harbor_agent_installed:AarInstalledAgent \
  --model anthropic/claude-sonnet-4-6
```

### Via job config (repeatable)

```bash
harbor run -c jobs/terminal_bench_aar.yaml
```

The config at `jobs/terminal_bench_aar.yaml`:

```yaml
datasets:
  - name: terminal-bench
    version: "2.0"

agents:
  - import_path: scripts.benchmarks.terminal_bench.harbor_agent_installed:AarInstalledAgent
    model_name: anthropic/claude-sonnet-4-6   # change model here

n_concurrent_trials: 1   # raise for parallel runs (needs more API quota)
```

> `datasets` and `agents` must be lists of objects — Harbor rejects shorthand
> strings like `terminal-bench@2.0` or flat keys like `agent_import_path`.

---

## Approach B — External agent

Aar runs on the host (inside the WSL distro) and calls the LLM API directly.
Every tool call is proxied into the container via `environment.exec()`.

```bash
harbor run \
  --dataset terminal-bench@2.0 \
  --agent-import-path scripts.benchmarks.terminal_bench.harbor_agent_external:AarExternalAgent \
  --model anthropic/claude-sonnet-4-6
```

> Because aar runs on the host, its own `~/.aar/config.json` safety settings
> apply. Approval gates must be off — the container is already the sandbox.
> The adapter sets `SafetyConfig(require_approval_for_writes=False,
> require_approval_for_execute=False)` automatically.

---

## Changing the model

`--model` / `model_name` accepts `provider/model-id`:

| Value | Provider | Model |
|---|---|---|
| `anthropic/claude-sonnet-4-6` | anthropic | claude-sonnet-4-6 |
| `anthropic/claude-opus-4-7` | anthropic | claude-opus-4-7 |
| `openai/gpt-4o` | openai | gpt-4o |
| `openai/o3` | openai | o3 |
| `ollama/gemma4:e4b` | ollama | gemma4:e4b |
| `ollama/qwen2.5-coder` | ollama | qwen2.5-coder |

For Ollama, `localhost` and `host.docker.internal` both fail with standalone
Docker Engine in WSL2. Use the Windows host IP (WSL2 default gateway) instead,
and configure Ollama to listen on `0.0.0.0` on Windows:

```bash
# On Windows: set OLLAMA_HOST=0.0.0.0 and restart Ollama

# In WSL:
WINDOWS_IP=$(ip route show default | awk '{print $3}')
export OLLAMA_BASE_URL=http://${WINDOWS_IP}:11434
curl ${OLLAMA_BASE_URL}/api/tags   # verify
```

---

## Viewing results

```bash
harbor view jobs              # browse all job results in the TUI
harbor trials list            # list completed trials
harbor trials view <id>       # inspect a single trial
```

Results are also written to `jobs/<timestamp>/` as JSON files.

---

## Troubleshooting

**`harbor: command not found`**
Harbor is installed by uv into `~/.local/bin`. Add it to PATH:
```bash
export PATH="$HOME/.local/bin:$PATH"
```

**`docker compose: unknown command`**
The Compose plugin is not installed:
```bash
sudo apt-get install -y docker-compose-v2
```

**`docker: permission denied` / `Cannot connect to Docker daemon`**
Either the daemon is not running or your user is not in the `docker` group:
```bash
sudo service docker start
# if group issue: sudo usermod -aG docker $USER  then restart WSL
```

**`ValidationError: datasets.0 — Input should be a valid dictionary`**
Harbor requires `datasets` to be a list of objects, not strings. Use:
```yaml
datasets:
  - name: terminal-bench
    version: "2.0"
```
Not `- terminal-bench@2.0`.

**`ModuleNotFoundError: No module named 'name_register'`**
The wrong `aar` package was installed from PyPI (a different project with the
same name). The installed agent builds a wheel from the local repo source and
uploads it to the container — no PyPI involved. If you see this error the
wheel build step failed silently; check that `pip wheel` works from the repo root:
```bash
pip wheel --no-deps -w /tmp/test-wheel .
```

**Agent hangs or all tool calls return "denied"**
Aar defaults to `require_approval_for_writes=True` and
`require_approval_for_execute=True`. In a headless container this causes
`console.input()` to block forever, and with no callback the policy silently
returns DENY. Both adapters disable these flags. If you supply a custom
`config.json`, make sure it includes:
```json
"safety": { "require_approval_for_writes": false, "require_approval_for_execute": false }
```

**First run is slow**
The installed agent builds a wheel, uploads it, and installs provider packages
on every fresh container — typically 2–4 minutes. Harbor caches the built
Docker image so subsequent runs of the same task are faster.
