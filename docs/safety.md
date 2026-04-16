# Safety Policy

Aar's safety system is a layered defense that controls what the agent can read, write, and execute. It operates at three levels: **denied-by-default patterns**, **policy decisions per tool call**, and **human approval gates**.

## How it works

Every tool call passes through the **SafetyPolicy** engine before execution:

```
Tool call → SafetyPolicy.check_tool() → ALLOW / DENY / ASK
                                              ↓
                                        if ASK → ApprovalCallback → APPROVED / DENIED
```

The policy evaluates rules in this order:

1. **Read-only mode** — if enabled, all write and execute side-effects are denied immediately
2. **Approval requirements** — if `require_approval_for_writes` or `require_approval_for_execute` is set, matching tools return ASK
3. **Path rules** — explicit `PathRule` entries (first match wins)
4. **Denied paths** — glob patterns that block file access
5. **Allowed paths** — if set, only matching paths are permitted (whitelist)
6. **Command rules** — explicit `CommandRule` entries for shell commands (first match wins)
7. **Denied commands** — substring patterns that block dangerous shell commands
8. If nothing matches, the tool call is **ALLOWED**

## Built-in defaults

### Denied paths (SafetyConfig)

These glob patterns are always blocked for file read/write tools, regardless of transport or flags:

| Category | Patterns |
|----------|----------|
| Unix system files | `/etc/shadow`, `/etc/passwd`, `/etc/sudoers`, `/etc/sudoers.d/**` |
| Environment files | `**/.env`, `**/.env.*` |
| Credentials | `**/credentials`, `**/credentials.*`, `**/secrets`, `**/secrets.*` |
| Key material | `**/*.pem`, `**/*.key`, `**/*.p12`, `**/*.pfx` |
| SSH | `**/.ssh/**`, `**/id_rsa`, `**/id_dsa`, `**/id_ecdsa`, `**/id_ed25519` |
| Cloud providers | `**/.aws/**`, `**/.azure/**`, `**/.config/gcloud/**` |
| Package manager tokens | `**/.netrc`, `**/.npmrc`, `**/.pypirc` |

### Denied commands (PolicyConfig)

These substring patterns block dangerous shell commands:

| Category | Patterns |
|----------|----------|
| Filesystem destruction | `rm -rf /`, `rm -rf /*`, `rm -rf ~`, `mkfs`, `dd if=`, `> /dev/sda` |
| System control | `shutdown`, `reboot`, `halt`, `poweroff`, `init 0`, `init 6` |
| Fork bomb | `:(){:\|:&};:` |
| Permission escalation | `chmod 777`, `chmod -R 777` |
| Remote code execution | `curl\|sh`, `curl \| sh`, `curl\|bash`, `curl \| bash`, `wget\|sh`, `wget \| sh`, `wget\|bash`, `wget \| bash` |
| Reverse shell | `nc -e`, `ncat -e` |
| History wipe | `history -c` |

Both lists can be extended via configuration (CLI flags, config files, or programmatic API) but not reduced below the defaults through CLI flags alone.

## Per-transport defaults

### `aar chat` and `aar tui` (interactive)

**Workspace sandbox ON by default:**

| Setting | Default | Effect |
|---------|---------|--------|
| `--require-approval` | **on** | Prompts before every write or shell command |
| `--restrict-to-cwd` | **on** | File tools can only access `cwd/**` |

This creates a two-layer defense:
- **File tools** are restricted to the current directory by `allowed_paths`
- **Bash** bypasses `allowed_paths` (it runs arbitrary commands), so it requires human approval instead

Disable the sandbox for trusted workflows:

```bash
aar chat --no-require-approval --no-restrict-to-cwd
```

### `aar run` (automation)

**Permissive by default:**

| Setting | Default | Effect |
|---------|---------|--------|
| `--require-approval` | **off** | No approval prompts |
| `--restrict-to-cwd` | **off** | File tools can access any non-denied path |

Opt in to the sandbox for untrusted tasks:

```bash
aar run "task" --require-approval --restrict-to-cwd
```

### `aar serve` (web API)

The web server accepts an optional `safety` field in the request body to override safety settings per request:

```json
{
  "prompt": "do something",
  "safety": {
    "read_only": true,
    "require_approval_for_writes": true
  }
}
```

By default, the web transport has **no approval callback** — any tool call that returns ASK is auto-denied (no human present). To add a human gate, implement an external approval flow via SSE events.

The `--read-only` flag is available on `aar serve` for global read-only mode.

## CLI flags reference

| Flag | Available on | Description |
|------|-------------|-------------|
| `--read-only` | chat, run, tui, serve | Block all write and execute tools |
| `--require-approval / --no-require-approval` | chat, run, tui | Prompt before write/execute tools |
| `--restrict-to-cwd / --no-restrict-to-cwd` | chat, run, tui | Restrict file tools to `cwd/**` |
| `--denied-paths TEXT` | chat, run, tui | Comma-separated globs appended to defaults |
| `--allowed-paths TEXT` | chat, run, tui | Comma-separated globs (overrides `--restrict-to-cwd`) |
| `--config PATH` | chat, run, tui, serve | Load full `AgentConfig` from a JSON file |

## Configuration file

Create a JSON file matching the `AgentConfig` schema:

```json
{
  "provider": {
    "name": "anthropic",
    "model": "claude-sonnet-4-6"
  },
  "safety": {
    "read_only": false,
    "require_approval_for_writes": true,
    "require_approval_for_execute": true,
    "denied_paths": ["**/.env", "**/secrets/**"],
    "allowed_paths": ["/home/user/project/**"],
    "sandbox": "auto",
    "sandbox_workspace": "/home/user/project"
  },
  "max_steps": 30,
  "timeout": 120.0
}
```

Load with:

```bash
aar chat --config aar.json
```

Or programmatically:

```python
from agent.core.config import load_config
from pathlib import Path

config = load_config(Path("aar.json"))
```

**Precedence** (lowest to highest): `PolicyConfig defaults` -> `config file (--config)` -> `explicit CLI flags`

## Approval callback

When a policy decision is ASK, the `ApprovalCallback` is invoked. The callback receives the tool spec and the tool call, and returns one of:

| Result | Meaning |
|--------|---------|
| `APPROVED` | Allow this specific call |
| `DENIED` | Block this specific call |
| `APPROVED_ALWAYS` | Allow this call and all future calls to the same tool |

The CLI and TUI transports use a terminal prompt:

```
+----------------------------------+
| Approval Required                |
|   bash                           |
|     command: rm -rf build/       |
+----------------------------------+
Allow? [y]es / [n]o / [a]lways:
```

### Custom approval callback

```python
from agent.safety.permissions import ApprovalResult

async def my_callback(spec, tool_call) -> ApprovalResult:
    # Your logic — Slack notification, web UI, auto-approve known tools, etc.
    return ApprovalResult.APPROVED

agent = Agent(config=config, approval_callback=my_callback)
```

## Sandbox modes

Aar's philosophy is that the agent works inside a configured workspace directory and should not write outside it. The sandbox is the OS-level enforcement layer for that boundary. It operates on top of the policy engine's `allowed_paths` check — the policy guards Aar's own tool calls, the sandbox guards anything that escapes through a subprocess.

> **Important distinction**: filesystem ACLs (`icacls`, `chmod`) control who can access a directory from *outside*. To restrict where a running *process* can go, you need OS-level mechanisms that act on the process itself — which is what the platform-native sandboxes provide.

### How the sandbox executes commands

All sandbox modes share the same underlying mechanism: they call `bash -c <command>`. The modes differ only in what they wrap around that call — restricted environment variables, resource limits, or OS-level process restrictions.

**On Windows this has a critical consequence:** the sandbox uses whatever `bash` binary is first on `PATH`. There are two common cases:

| `bash` on PATH | Where commands run | Package installs go |
|---|---|---|
| WSL2 (`C:\Windows\System32\wsl.exe` resolves first) | Inside WSL2 Linux environment | WSL2 filesystem (`/home/...`, `/tmp/...`) |
| Git Bash (`C:\Program Files\Git\bin\bash.exe`) | Native Windows filesystem via MSYS2 | Windows filesystem (POSIX paths translated) |

These are completely separate runtime environments. A `pip install pip-install-test` that runs in WSL2 installs into WSL2's Python — invisible to a native Windows Python process and vice versa. Run `which bash` inside a shell tool call to confirm which environment your agent is using.

### Available modes

| Mode | On Windows | On Linux |
|------|-----------|---------|
| `local` | `bash -c cmd` — inherits full parent env, no restrictions | `bash -c cmd` — inherits full parent env, no restrictions |
| `subprocess` | `bash -c cmd` + restricted env vars (only essential vars passed in) | `bash -c cmd` + restricted env vars + `ulimit -v` memory cap |
| `workspace` | `bash -c cmd` + restricted env vars *(Landlock silently skipped — aar process is win32)* | `bash -c cmd` + restricted env vars + `ulimit -v` + **Landlock LSM** (write-restricted to workspace) |
| `windows` | `bash -c cmd` + restricted env vars + **Job Object** (memory/process limits) + **Low Integrity Level** | N/A — treated as `subprocess` |
| `auto` | → `windows` | → `workspace` (with Landlock if kernel ≥ 5.13) |

### Choosing a mode

```
Trusted local dev   →  local       (default — no overhead)
CI / scripted runs  →  subprocess  (light isolation, cross-platform)
Production Linux    →  workspace   (strongest — Landlock is kernel-enforced)
Production Windows  →  windows     (Job Objects + Low Integrity)
Any production      →  auto        (picks the best available for the platform)
```

### Limits of each mode

| Mode | Filesystem restriction | Memory cap | Process count cap | Network restriction |
|------|----------------------|-----------|-------------------|---------------------|
| `local` | None | None | None | None |
| `subprocess` | None | Unix only (`ulimit`) | None | None |
| `workspace` | **Linux only** — write-blocked outside workspace via Landlock | Unix only (`ulimit`) | None | None |
| `windows` | **Partial** — Low Integrity cannot write to Medium/High locations; workspace is stamped writable | Yes — Job Object working-set limit | Yes — Job Object active process cap | None |

No sandbox mode restricts outbound network access. If you need network isolation, use a container-based approach (see [Docker-free sandboxing](#docker-free-sandboxing-on-windows) below) or firewall rules outside Aar.

The `workspace` mode on Windows is effectively `subprocess` — the Landlock `preexec_fn` is a Linux-only API and is silently skipped when the aar process itself is a native Windows Python process (even if `bash` resolves to WSL2). To get Landlock on Windows, run aar inside WSL2 directly.

### `workspace` — Linux Landlock (recommended for Linux)

Landlock is a kernel security module (Linux ≥ 5.13) that lets an unprivileged process restrict its own filesystem access before spawning a child. After `landlock_restrict_self()` the spawned subprocess literally cannot call `open()` on files outside the allowed paths — the kernel refuses the syscall. No root, no container, no daemon required.

**What it enforces:**
- Subprocess can **read and execute** from anywhere on the filesystem (needed for tools, libraries, etc.)
- Subprocess can **only write** within the configured workspace directory
- Memory cap via `ulimit -v` (same as `subprocess` mode)
- Restricted environment variables

**Fallback:** If Landlock is unavailable (kernel < 5.13, LSM disabled), a warning is logged and the sandbox falls back to environment restriction + `ulimit` only.

**Configuration:**

```json
{
  "safety": {
    "sandbox": "workspace",
    "sandbox_workspace": "/home/user/project",
    "sandbox_max_memory_mb": 512
  }
}
```

```python
from agent.core.config import SafetyConfig

safety = SafetyConfig(
    sandbox="workspace",
    sandbox_workspace="/home/user/project",
    sandbox_max_memory_mb=512,
)
```

If `sandbox_workspace` is not set, it defaults to the current working directory at runtime.

**Smoke test** (verify Landlock is blocking writes outside workspace):

```bash
python -c "
from agent.safety.sandbox import WorkspaceSandbox
import asyncio

sb = WorkspaceSandbox(workspace='/tmp/my_workspace')
# Should be blocked — /etc/passwd is outside workspace
r = asyncio.run(sb.execute('echo test > /etc/test_aar'))
print('blocked' if r.exit_code != 0 else 'NOT blocked — landlock unavailable')
"
```

### `windows` — Windows Job Objects + Low Integrity (recommended for Windows)

Windows has no equivalent of Landlock. The `windows` mode layers two mechanisms:

**1. Job Object** (via `ctypes kernel32`):
- Enforces working-set memory limit (`sandbox_max_memory_mb`, default 512 MB)
- Caps the number of active child processes (`sandbox_max_processes`, default 10)
- `KILL_ON_JOB_CLOSE` — orphaned processes in the job are killed automatically when the agent exits

**2. Low Integrity Level** (optional, `sandbox_use_low_integrity: true` by default):
- The subprocess runs at Windows Mandatory Integrity Level *Low* (the same level as IE Protected Mode and sandboxed browser tabs)
- A Low-integrity process **cannot write to** Medium/High-integrity locations: user profile (`C:\Users\<you>`), `C:\Program Files`, registry
- The workspace is stamped as Low-integrity-writable via `icacls /setintegritylevel Low` so the subprocess *can* write there
- If the integrity-level helper fails (rare: policy, UAC edge cases), the sandbox falls back to Job Object only and logs a warning

**`icacls` role clarification**: `icacls` here is used correctly — it grants the Low-integrity subprocess write access *to the workspace*, not to restrict it. The restriction comes from the Low Integrity token.

**Configuration:**

```json
{
  "safety": {
    "sandbox": "windows",
    "sandbox_workspace": "C:/Users/user/project",
    "sandbox_max_memory_mb": 512,
    "sandbox_max_processes": 10,
    "sandbox_use_low_integrity": true
  }
}
```

```python
safety = SafetyConfig(
    sandbox="windows",
    sandbox_workspace="C:/Users/user/project",
    sandbox_max_memory_mb=512,
    sandbox_max_processes=10,
    sandbox_use_low_integrity=True,
)
```

Disable Low Integrity if you hit permission issues (rare) while keeping Job Object limits:

```json
{
  "safety": {
    "sandbox": "windows",
    "sandbox_use_low_integrity": false
  }
}
```

### `auto` — pick best available (recommended for production)

```json
{
  "safety": {
    "sandbox": "auto",
    "sandbox_workspace": "/home/user/project"
  }
}
```

Selection logic:

| Platform | Selected mode |
|----------|--------------|
| Linux (`sys.platform.startswith("linux")`) | `workspace` (Landlock + ulimit) |
| Windows (`os.name == "nt"`) | `windows` (Job Object + Low Integrity) |
| macOS / other Unix | `subprocess` (env restriction + no OS-level filesystem restriction) |

### `subprocess` — cross-platform baseline

Available everywhere, provides:
- Restricted environment variables (only `PATH`, `HOME`, `TERM`, `LANG` + Windows essentials)
- `ulimit -v` memory cap on Unix (skipped on Windows — `ulimit` is unreliable in Git Bash/WSL context)
- Command timeout

**Limits:** No filesystem-level restriction — a subprocess can still read and write anywhere the user has permission. The only protection is that leaked credentials from the parent environment are not forwarded.

### SafetyConfig sandbox fields reference

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `sandbox` | `str` | `"local"` | Sandbox mode: `local` \| `subprocess` \| `workspace` \| `windows` \| `wsl` \| `auto` |
| `sandbox_max_memory_mb` | `int` | `512` | Memory limit (MB) for `subprocess`, `workspace`, and `windows` modes |
| `sandbox_max_processes` | `int` | `10` | Max active child processes — Windows Job Object only |
| `sandbox_workspace` | `str \| None` | `None` (→ cwd) | Workspace root path. Windows paths are auto-translated to `/mnt/…` for `wsl` mode |
| `sandbox_use_low_integrity` | `bool` | `True` | Windows: run subprocess at Low integrity level |
| `sandbox_shell_path` | `str` | `""` | Path to bash binary for `local`/`subprocess`/`windows`/`workspace` modes (empty = auto-detect) |
| `sandbox_wsl_distro` | `str` | `"aar-sandbox"` | WSL2 distro name for `wsl` mode |
| `sandbox_wsl_shell` | `str` | `"sh"` | Shell binary inside the distro (`sh` works on minimal Alpine; use `bash` if installed) |
| `sandbox_wsl_install_path` | `str \| None` | `None` | Where to store the distro data (default: `%LOCALAPPDATA%\aar\wsl-distros\<distro>`) |
| `sandbox_wsl_rootfs_url` | `str` | Alpine latest-stable | URL of the rootfs tarball used by `aar sandbox setup` |
| `sandbox_wsl_packages` | `list[str]` | `["python3", "py3-pip"]` | Packages to install during `aar sandbox setup` |

### Shell tool wiring

The sandbox is applied to **all shell commands** — both the built-in `bash` tool and any commands spawned by subprocesses. The `bash` tool handler delegates execution to `sandbox.execute()`, which applies the platform-appropriate isolation before the process is spawned.

This means `sandbox="local"` is the only mode that provides no isolation. All other modes enforce their restrictions even for one-liner `bash` tool calls.

## Docker-free sandboxing on Windows

If you need strong process isolation with multi-language package support (not just Python), there are two practical options that do not require Docker Desktop.

### Option A: Dedicated WSL2 distro — `sandbox = "wsl"` (recommended)

Aar includes a first-class `wsl` sandbox mode backed by `WslDistroSandbox`. Commands run via
`wsl -d <distro> -- sh -c <cmd>`, completely isolated from your main WSL2 environment. The
distro is tiny, disposable, and can host any language.

**What you get:**
- Filesystem isolation from your main WSL2 distro and native Windows
- Any language installable via the distro's package manager (`apk`, `apt`, …)
- Package installs stay inside the distro — the host is untouched
- Windows path translation is automatic (`B:\foo` → `/mnt/b/foo`)
- No Docker Desktop, no daemon, no license cost

#### 1. Configure (optional — defaults work out of the box)

Edit `~/.aar/config.json` to set sandbox parameters before running setup:

```json
{
  "safety": {
    "sandbox": "wsl",
    "sandbox_wsl_distro": "aar-sandbox",
    "sandbox_wsl_packages": ["python3", "py3-pip", "nodejs", "npm"],
    "sandbox_wsl_rootfs_url": "https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/x86_64/alpine-minirootfs-3.21.0-x86_64.tar.gz"
  }
}
```

All `sandbox_wsl_*` fields are optional. The defaults use Alpine Linux with Python 3 and pip.

#### 2. Set up the distro

```bash
# Reads defaults from ~/.aar/config.json — no flags required
aar sandbox setup

# Override packages for this run only (comma-separated)
aar sandbox setup --packages "python3,py3-pip,nodejs,npm"

# Use a different distro name
aar sandbox setup --distro my-sandbox

# Force-recreate an existing distro
aar sandbox setup --force
```

`setup` downloads the rootfs (~3 MB for Alpine), imports it as a dedicated WSL2 distro, and
installs the configured packages. It prints a config snippet at the end.

#### 3. Verify

```bash
aar sandbox status
# WSL2 available : yes
# Distro exists  : yes
# 5.15.153.1-microsoft-standard-WSL2
# Python 3.12.3
```

#### 4. Run the agent

Once `sandbox = "wsl"` is in config, `aar chat` / `aar run` automatically route all `bash`
tool calls through the sandbox distro. No other changes needed.

#### Reset / teardown

Wipes all installed packages and distro state. Workspace files on the Windows filesystem are
not affected.

```bash
aar sandbox reset              # prompts for distro name to confirm
aar sandbox reset --yes        # skip confirmation
```

#### Using a non-Alpine rootfs

Point `sandbox_wsl_rootfs_url` at any `.tar.gz` rootfs (Ubuntu, Debian, etc.) and update
`sandbox_wsl_packages` to use that distro's package manager:

```json
{
  "safety": {
    "sandbox_wsl_rootfs_url": "https://cloud-images.ubuntu.com/wsl/releases/24.04/current/ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz",
    "sandbox_wsl_packages": ["python3", "python3-pip", "nodejs", "npm"]
  }
}
```

> **Note:** The `apk add` command in `setup` is Alpine-specific. For other distros, install packages manually after import:
> `wsl -d my-sandbox -- apt-get install -y python3 python3-pip`

**Limits of the WSL2 distro approach:**
- No outbound network restriction (WSL2 distros share the host network adapter)
- Files in the workspace are accessed via `/mnt/<drive>/...` — this is automatic when `sandbox_workspace` is a Windows path
- The distro persists between agent sessions — use `aar sandbox reset` to wipe accumulated state
- `sandbox_wsl_shell` controls which shell runs commands inside the distro (default: `sh`; set to `bash` if you install it)

---

### Option B: Podman (rootless OCI containers, no daemon)

Podman is a Docker-compatible CLI that runs OCI containers without a daemon and without requiring Docker Desktop. It runs inside WSL2 and supports the full OCI image ecosystem.

**Install inside your main WSL2 distro:**

```bash
# Ubuntu/Debian in WSL2
sudo apt-get install -y podman

# Verify
podman run --rm alpine echo "sandbox works"
```

**How the agent would use it** (future `PodmanSandbox` / `DockerSandbox` mode):

```bash
podman run --rm \
  --network none \                        # no internet access
  --memory 512m \                         # memory cap
  -v /mnt/b/Github_my/aar:/workspace \   # workspace mounted read-write
  -w /workspace \
  python:3.12-slim \
  bash -c "pip install pip-install-test && python script.py"
```

This gives the strongest possible isolation — the command runs in a fresh container with a clean filesystem. Packages installed with `pip install` live only inside that container run.

**Limits of the Podman approach:**
- Cold start per `podman run` call adds ~1–2 s overhead (use `podman exec` into a persistent container to avoid this)
- Requires Podman installed inside WSL2 — an extra setup step
- The `DockerSandbox`/`PodmanSandbox` backend is not yet implemented in Aar (contributions welcome — see `agent/safety/sandbox.py`)

---

### Comparison

| Approach | Isolation strength | Setup effort | Multi-language | Network isolation | State persistence |
|---|---|---|---|---|---|
| `windows` mode (Job Object + Low IL) | Medium | None (built-in) | Depends on PATH | No | N/A |
| Dedicated WSL2 distro | Strong | Low | Yes | No | Persists (resetable) |
| Podman (WSL2) | Strongest | Medium | Yes (any image) | Yes (`--network none`) | Ephemeral per run |

## Architecture

The safety system has four components:

- **`agent/safety/policy.py`** — `SafetyPolicy` evaluates tool calls against `PolicyConfig` rules, returning ALLOW/DENY/ASK
- **`agent/safety/permissions.py`** — `PermissionManager` handles ASK decisions by calling the approval callback and caching APPROVED_ALWAYS results
- **`agent/safety/sandbox.py`** — `LocalSandbox`, `SubprocessSandbox`, `WorkspaceSandbox`, `WindowsSubprocessSandbox`, and `WslDistroSandbox` control how shell commands are actually executed
- **`agent/safety/wsl_manager.py`** — helpers for WSL2 distro lifecycle (`is_wsl_available`, `list_distros`, `import_distro`, `unregister_distro`, `run_in_distro`, `download_rootfs`); used by `aar sandbox` commands
- **`agent/tools/builtin/shell.py`** — the `bash` tool handler delegates to the configured sandbox via a closure injected at registration time

These are composed by `ToolExecutor` (`agent/tools/execution.py`), which is the single entry point for all tool execution in the agent loop.
