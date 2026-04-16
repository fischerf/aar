# Safety Policy

Aar's safety system is a layered defense that controls what the agent can read, write, and execute. It operates at three levels: **denied-by-default patterns**, **policy decisions per tool call**, and **human approval gates**. The layer below those — **OS-level sandboxing of shell commands** — is covered in the [Sandbox modes](#sandbox-modes) section and [`sandbox_architecture.md`](sandbox_architecture.md).

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
    "sandbox": {
      "mode": "auto",
      "linux":   { "workspace": "/home/user/project" },
      "windows": { "workspace": "C:/Users/user/project" }
    }
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

The sandbox is the OS-level enforcement layer that wraps shell commands. It operates on top of the policy engine's `allowed_paths` check — the policy guards Aar's own tool calls, the sandbox guards anything that escapes through a subprocess.

> **Important distinction**: filesystem ACLs (`icacls`, `chmod`) control who can access a directory from *outside*. To restrict where a running *process* can go, you need OS-level mechanisms that act on the process itself — which is what the platform-native sandboxes provide.

For the full execution-path diagrams and honest per-mode strength/weakness analysis, see [`sandbox_architecture.md`](sandbox_architecture.md). This section is the configuration reference.

### Available modes

| Mode | Platform | Mechanism |
|------|----------|-----------|
| `local` | all | No isolation — plain `bash -c cmd` |
| `linux` | Linux ≥ 5.13 | Landlock LSM (write-restricted to workspace) + `ulimit -v` memory cap |
| `windows` | Windows | Job Object (memory/process caps) + Low Integrity Level (write-restricted) |
| `wsl` | Windows | Dedicated WSL2 distro (`wsl -d <distro> -- sh -c <cmd>`) |
| `auto` | all | Picks `linux` on Linux, `windows` on Windows, `local` elsewhere |

### Choosing a mode

```
Trusted local dev            →  local    (default, no overhead)
Linux production             →  linux    (strongest — Landlock is kernel-enforced)
Windows production           →  windows  (Job Object + Low Integrity)
Windows, multi-language      →  wsl      (disposable Alpine/Ubuntu execution environment)
Any production, cross-plat   →  auto     (picks best available for the platform)
macOS                        →  local    (no OS-level sandbox available)
```

### What each mode actually restricts

| Mode | Writes blocked outside workspace? | Reads blocked? | Resource caps | Network isolation |
|------|----------------------------------|----------------|---------------|-------------------|
| `local` | no | no | none | no |
| `linux` | **yes** — kernel-enforced via Landlock (Linux ≥ 5.13) | no (Landlock v1 doesn't restrict reads) | `ulimit -v` memory cap | no |
| `windows` | **mostly** — Low IL blocks writes to user profile, Program Files, HKCU; workspace stamped Low-writable | no — Low IL is write-side only | Job Object memory + process count | no |
| `wsl` | **no** — entire Windows filesystem auto-mounted at `/mnt/<drive>/` | no | none | no |

**No sandbox mode restricts outbound network access.** For network isolation, a container-based sandbox is required — see [`docker_sandbox_plan.md`](docker_sandbox_plan.md) for the planned additive Docker layer (runs via WSL-native Docker, no Docker Desktop required).

### `linux` — Linux Landlock (recommended for Linux)

Landlock is a kernel security module (Linux ≥ 5.13) that lets an unprivileged process restrict its own filesystem access before spawning a child. After `landlock_restrict_self()` the spawned subprocess literally cannot call `openat(O_WRONLY, ...)` on files outside the allowed paths — the kernel refuses the syscall. No root, no container, no daemon required.

**What it enforces:**
- Subprocess can **read and execute** from anywhere on the filesystem (needed for tools, libraries, etc.)
- Subprocess can **only write** within the configured workspace directory
- Memory cap via `ulimit -v`
- Restricted environment variables (only `PATH`, `HOME`, `TERM`, `LANG`)

**Fallback:** If Landlock is unavailable (kernel < 5.13, LSM disabled), a warning is logged and the sandbox falls back to environment restriction + `ulimit` only.

**Configuration:**

```json
{
  "safety": {
    "sandbox": {
      "mode": "linux",
      "linux": {
        "workspace": "/home/user/project",
        "max_memory_mb": 512
      }
    }
  }
}
```

```python
from agent.core.config import SafetyConfig, SandboxConfig, LinuxSandboxConfig

safety = SafetyConfig(
    sandbox=SandboxConfig(
        mode="linux",
        linux=LinuxSandboxConfig(workspace="/home/user/project", max_memory_mb=512),
    )
)
```

If `workspace` is not set, it defaults to the current working directory at runtime.

**Smoke test** (verify Landlock is blocking writes outside workspace):

```bash
python -c "
from agent.safety.sandbox import LinuxSandbox
import asyncio

sb = LinuxSandbox(workspace='/tmp/my_workspace')
# Should be blocked — /etc is outside workspace
r = asyncio.run(sb.execute('echo test > /etc/test_aar'))
print('blocked' if r.exit_code != 0 else 'NOT blocked — landlock unavailable')
"
```

### `windows` — Windows Job Objects + Low Integrity (recommended for Windows)

Windows has no equivalent of Landlock. The `windows` mode layers two mechanisms:

**1. Job Object** (via `ctypes kernel32`):
- Enforces working-set memory limit (`windows.max_memory_mb`, default 512 MB)
- Caps the number of active child processes (`windows.max_processes`, default 10)
- `KILL_ON_JOB_CLOSE` — orphaned processes in the job are killed automatically when the agent exits

**2. Low Integrity Level** (optional, `windows.use_low_integrity: true` by default):
- The subprocess runs at Windows Mandatory Integrity Level *Low* (the same level as IE Protected Mode and sandboxed browser tabs)
- A Low-integrity process **cannot write to** Medium/High-integrity locations: user profile (`C:\Users\<you>`), `C:\Program Files`, registry
- The workspace is stamped as Low-integrity-writable via `icacls /setintegritylevel Low` so the subprocess *can* write there
- If the integrity-level helper fails (rare: policy, UAC edge cases), the sandbox falls back to Job Object only and logs a warning

**`icacls` role clarification**: `icacls` here is used correctly — it grants the Low-integrity subprocess write access *to the workspace*, not to restrict it. The restriction comes from the Low Integrity token.

**Configuration:**

```json
{
  "safety": {
    "sandbox": {
      "mode": "windows",
      "windows": {
        "workspace": "C:/Users/user/project",
        "max_memory_mb": 512,
        "max_processes": 10,
        "use_low_integrity": true
      }
    }
  }
}
```

```python
from agent.core.config import SafetyConfig, SandboxConfig, WindowsSandboxConfig

safety = SafetyConfig(
    sandbox=SandboxConfig(
        mode="windows",
        windows=WindowsSandboxConfig(
            workspace="C:/Users/user/project",
            max_memory_mb=512,
            max_processes=10,
            use_low_integrity=True,
        ),
    )
)
```

Disable Low Integrity if you hit permission issues (rare) while keeping Job Object limits:

```json
{
  "safety": {
    "sandbox": {
      "mode": "windows",
      "windows": { "use_low_integrity": false }
    }
  }
}
```

### `wsl` — dedicated WSL2 distro

A dedicated, disposable WSL2 distro is used as the execution environment. Commands run via `wsl -d <distro> -- sh -c <cmd>`, isolated from your main WSL2 setup and the host Python installation. The distro is managed by `aar sandbox setup / status / reset`.

**What it isolates:**
- Distro filesystem (`/etc`, `/usr`, `/home`, installed packages) is separate from host Windows and any other WSL2 distros
- `apk add` / `pip install` stays inside the distro — host is untouched
- State is resettable via `aar sandbox reset`

**What it does NOT isolate (important):**
- The entire Windows filesystem is auto-mounted at `/mnt/<drive>/` by WSL2. `rm -rf /mnt/c/Users/you` is just as effective as running it natively.
- No outbound network restriction (WSL2 shares the host network)
- No memory or process count cap
- The agent runs as **root** inside the distro

**Use this mode for:** a clean, wipeable multi-language execution environment (install Node, Go, Rust, etc. without polluting your host). **Not suitable for:** protecting against a malicious command — use `windows` mode for that, or wait for the planned Docker layer.

**Configuration** (defaults work out of the box once `aar sandbox setup` has been run):

```json
{
  "safety": {
    "sandbox": {
      "mode": "wsl",
      "wsl": {
        "distro": "aar-sandbox",
        "shell": "sh",
        "packages": ["python3", "py3-pip", "nodejs", "npm"],
        "rootfs_url": "https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/x86_64/alpine-minirootfs-3.21.0-x86_64.tar.gz"
      }
    }
  }
}
```

All `wsl` sub-fields are optional. Defaults use Alpine Linux with Python 3 and pip.

#### Managing the distro — `aar sandbox`

The `aar sandbox` sub-app owns the WSL2 distro lifecycle:

```bash
aar sandbox setup                          # downloads rootfs, imports distro, installs packages
aar sandbox setup --force                  # unregister existing + recreate
aar sandbox setup --packages "python3,py3-pip,nodejs,npm"  # override packages
aar sandbox setup --distro my-sandbox      # custom distro name

aar sandbox status                         # show distro state (exists, kernel, Python version)

aar sandbox reset                          # unregister + recreate, prompts for confirmation
aar sandbox reset --yes                    # skip confirmation
```

All flags on `setup` and `reset` are optional overrides — primary values come from `~/.aar/config.json` (`safety.sandbox.wsl.*`).

`setup` downloads the rootfs (~3 MB for Alpine), imports it as a dedicated WSL2 distro, and installs the configured packages. It prints a config snippet at the end.

**Reset behavior:** unregisters the distro, re-downloads rootfs, reinstalls packages. Workspace files on the Windows filesystem (`/mnt/<drive>/...`) are **not affected** — only the distro's own filesystem is wiped.

#### Using a non-Alpine rootfs

Point `wsl.rootfs_url` at any `.tar.gz` rootfs (Ubuntu, Debian, etc.) and update `wsl.packages` to use that distro's package manager:

```json
{
  "safety": {
    "sandbox": {
      "mode": "wsl",
      "wsl": {
        "rootfs_url": "https://cloud-images.ubuntu.com/wsl/releases/24.04/current/ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz",
        "packages": ["python3", "python3-pip", "nodejs", "npm"]
      }
    }
  }
}
```

> **Note:** The `apk add` command in `setup` is Alpine-specific. For other distros, install packages manually after import:
> `wsl -d my-sandbox -- apt-get install -y python3 python3-pip`

#### Windows program execution through the `wsl` sandbox

The agent can invoke native Windows executables from inside the distro via WSL2 interop:

- `notepad.exe`, `python.exe`, `git.exe`, `powershell.exe` — work directly
- Console tools (`where.exe`, `ipconfig.exe`, `cmd.exe /c "<cmd>"`) — work, with stdout piped back
- Windows paths must be translated to `/mnt/<drive>/...` (automatic for `cwd`; manual in command arguments)
- `.bat` files must be at `/mnt/...` paths and invoked via `cmd.exe /c <path>`

### `auto` — pick best available

```json
{
  "safety": {
    "sandbox": {
      "mode": "auto",
      "linux":   { "workspace": "/home/user/project" },
      "windows": { "workspace": "C:/Users/user/project" }
    }
  }
}
```

Selection logic:

| Platform | Selected mode |
|----------|--------------|
| Linux (`sys.platform.startswith("linux")`) | `linux` (Landlock + ulimit) |
| Windows (`os.name == "nt"`) | `windows` (Job Object + Low Integrity) |
| macOS / other Unix | `local` (no OS-level sandbox available) |

### `local` — no sandbox

Direct subprocess execution with no restrictions — inherits the full parent environment and user permissions. This is the default and the right choice for trusted local development.

```json
{
  "safety": {
    "sandbox": { "mode": "local" }
  }
}
```

### Sandbox configuration reference

`safety.sandbox` is a nested object with a `mode` field and one sub-object per sandbox type. Only the sub-object matching the active `mode` is used — all other sub-objects are ignored.

**Top-level `SandboxConfig`:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mode` | `str` | `"local"` | Active mode: `local` \| `linux` \| `windows` \| `wsl` \| `auto` |
| `local` | `LocalSandboxConfig` | — | Settings for `local` mode (no options) |
| `linux` | `LinuxSandboxConfig` | — | Settings for `linux` mode |
| `windows` | `WindowsSandboxConfig` | — | Settings for `windows` mode |
| `wsl` | `WslSandboxConfig` | — | Settings for `wsl` mode |

**`LinuxSandboxConfig`:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `workspace` | `str \| None` | `None` (→ cwd) | Workspace root path restricted by Landlock |
| `max_memory_mb` | `int` | `512` | Memory cap via `ulimit -v` |

**`WindowsSandboxConfig`:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `workspace` | `str \| None` | `None` (→ cwd) | Workspace root path stamped Low-integrity-writable |
| `max_memory_mb` | `int` | `512` | Working-set limit via Job Object |
| `max_processes` | `int` | `10` | Max active child processes — Job Object |
| `use_low_integrity` | `bool` | `True` | Run subprocess at Windows Low Integrity level |

**`WslSandboxConfig`:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `distro` | `str` | `"aar-sandbox"` | WSL2 distro name |
| `shell` | `str` | `"sh"` | Shell binary inside the distro (`sh` works on minimal Alpine) |
| `workspace` | `str \| None` | `None` (→ cwd) | Windows path — auto-translated to `/mnt/…` |
| `install_path` | `str \| None` | `None` | Where to store distro data (default: `%LOCALAPPDATA%\aar\wsl-distros\<distro>`) |
| `rootfs_url` | `str` | Alpine latest-stable | Rootfs tarball URL used by `aar sandbox setup` |
| `packages` | `list[str]` | `["python3", "py3-pip"]` | Packages installed during `aar sandbox setup` |

### Shell tool wiring

The sandbox is applied to **all shell commands** — both the built-in `bash` tool and any commands spawned by subprocesses. The `bash` tool handler delegates execution to `sandbox.execute()`, which applies the platform-appropriate isolation before the process is spawned.

This means `mode: "local"` is the only setting that provides no isolation. All other modes enforce their restrictions even for one-liner `bash` tool calls.

## Future — container-based sandboxing

None of the current modes restrict outbound network access, and the `wsl` mode does not restrict host filesystem access via `/mnt/`. For stronger isolation, an optional Docker layer is planned that runs containers inside a WSL2 distro with Docker natively installed (no Docker Desktop required) — see [`docker_sandbox_plan.md`](docker_sandbox_plan.md).

## Architecture

The safety system has four components:

- **`agent/safety/policy.py`** — `SafetyPolicy` evaluates tool calls against `PolicyConfig` rules, returning ALLOW/DENY/ASK
- **`agent/safety/permissions.py`** — `PermissionManager` handles ASK decisions by calling the approval callback and caching APPROVED_ALWAYS results
- **`agent/safety/sandbox.py`** — `LocalSandbox`, `LinuxSandbox`, `WindowsSubprocessSandbox`, and `WslDistroSandbox` control how shell commands are actually executed
- **`agent/safety/wsl_manager.py`** — helpers for WSL2 distro lifecycle (`is_wsl_available`, `list_distros`, `import_distro`, `unregister_distro`, `run_in_distro`, `download_rootfs`); used by `aar sandbox` commands
- **`agent/tools/builtin/shell.py`** — the `bash` tool handler delegates to the configured sandbox via a closure injected at registration time

These are composed by `ToolExecutor` (`agent/tools/execution.py`), which is the single entry point for all tool execution in the agent loop.
