# Terminal-Bench 2.0 — Setup & Run

## Overview

`install_terminal_bench.sh` is a one-shot setup script that installs everything
needed to run [Terminal-Bench 2.0](https://www.harborframework.com) against aar
inside a WSL2 distro.

After setup, running the benchmark is a few commands.

---

## Step-by-step

### 1. Set up the WSL distro (Windows — run once)

If you haven't already set up the aar sandbox:

```powershell
aar sandbox setup
```

Or wipe and recreate an existing one:

```powershell
aar sandbox reset --yes
```

This creates and configures the WSL2 distro (typically `aar-ubuntu`).

---

### 2. Enter the distro

```powershell
wsl -d aar-ubuntu
```

All remaining steps run **inside** this WSL shell.

---

### 3. Run the install script (once per distro)

From the repo root inside WSL:

```bash
cd /mnt/b/Github_my/aar
bash scripts/benchmarks/install_terminal_bench.sh
```

This installs (skipping anything already present):

| Step | What |
|---|---|
| 0 | Verifies WSL |
| 1 | Docker engine |
| 2 | Docker Compose plugin (`docker compose`) |
| 3 | Python 3.11+ |
| 4 | uv + adds `~/.local/bin` to `~/.bashrc` |
| 5 | Harbor (`uv tool install harbor`) |
| 6 | aar editable install + venv added to `~/.bashrc` |
| 7 | `jobs/terminal_bench_aar.yaml` job config |
| 8 | Prints a quick-start cheat sheet |

The script writes PATH and venv activation to `~/.bashrc`, but those changes
only take effect in new shells. **In the same shell where you ran the script**,
activate them manually before continuing:

```bash
source /mnt/b/Github_my/aar/.venv/bin/activate
export PATH="$HOME/.local/bin:$PATH"
```

---

### 4. Set your API key / provider URL

```bash
export ANTHROPIC_API_KEY=sk-ant-...
# or
export OPENAI_API_KEY=sk-...
```

**For Ollama** the setup is slightly more involved because Ollama runs on Windows
but the agent runs inside a Docker container in WSL2:

**Step 1 — Windows:** configure Ollama to listen on all interfaces (not just
`127.0.0.1`). In Windows environment variables set:
```
OLLAMA_HOST=0.0.0.0
```
Then restart Ollama from the system tray.

**Step 2 — WSL:** get the Windows host IP (the WSL2 default gateway) and export it:
```bash
WINDOWS_IP=$(ip route show default | awk '{print $3}')
export OLLAMA_BASE_URL=http://${WINDOWS_IP}:11434

# verify Ollama is reachable:
curl ${OLLAMA_BASE_URL}/api/tags
```

The install script detects and prints this IP for you at the end.

> **Why not `localhost` or `host.docker.internal`?**
> `localhost` inside WSL resolves to the WSL2 VM itself, not Windows.
> `host.docker.internal` is only auto-configured by Docker Desktop — with the
> standalone Docker Engine installed by this script it does not exist inside
> containers. The Windows gateway IP works from both WSL and Docker containers.

---

### 5. Run the benchmark

```bash
cd /mnt/b/Github_my/aar
harbor run -c jobs/terminal_bench_aar.yaml
```

Edit `jobs/terminal_bench_aar.yaml` to change the model or number of parallel
trials (`n_concurrent_trials`). See
`scripts/benchmarks/terminal_bench/README.md` for the full reference.

---

### 6. View results

```bash
harbor view jobs
```

Or on the command line:

```bash
harbor trials list
harbor trials view <id>
```

Raw JSON results are written to `jobs/<timestamp>/`.

---

## After the first run

Steps 1–3 are one-time setup. For every subsequent benchmark run:

**In the same shell as the install script** (PATH and venv not yet sourced):
```bash
source /mnt/b/Github_my/aar/.venv/bin/activate
export PATH="$HOME/.local/bin:$PATH"
export ANTHROPIC_API_KEY=sk-ant-...
cd /mnt/b/Github_my/aar
harbor run -c jobs/terminal_bench_aar.yaml
```

**In a fresh `wsl -d aar-ubuntu` shell** (`~/.bashrc` sources everything automatically):
```bash
export ANTHROPIC_API_KEY=sk-ant-...
cd /mnt/b/Github_my/aar
harbor run -c jobs/terminal_bench_aar.yaml
```
