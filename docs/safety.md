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

### Available modes

| Mode | Platform | Description |
|------|----------|-------------|
| `local` | all | Direct subprocess, no isolation (default — trusted dev environments) |
| `subprocess` | all | Restricted env vars + `ulimit -v` memory cap on Unix; env restriction only on Windows |
| `workspace` | Linux | **Landlock LSM** restricts subprocess to workspace — read/execute anywhere, write only in workspace — plus `ulimit`. Requires kernel ≥ 5.13. |
| `windows` | Windows | **Job Object** resource limits + **Low Integrity Level** so subprocess cannot write outside the workspace |
| `auto` | all | Selects `workspace` on Linux, `windows` on Windows, `subprocess` elsewhere |

### Choosing a mode

```
Trusted local dev   →  local   (default — no overhead)
CI / scripted runs  →  subprocess  (light isolation, cross-platform)
Production Linux    →  workspace   (strongest — Landlock is kernel-enforced)
Production Windows  →  windows     (Job Objects + Low Integrity)
Any production      →  auto        (picks the best available for the platform)
```

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
- `ulimit -v` memory cap on Unix (skipped on Windows)
- Command timeout

No filesystem-level restriction — a subprocess can still reach outside the workspace.

### SafetyConfig sandbox fields reference

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `sandbox` | `str` | `"local"` | Sandbox mode: `local` \| `subprocess` \| `workspace` \| `windows` \| `auto` |
| `sandbox_max_memory_mb` | `int` | `512` | Memory limit (MB) for `subprocess`, `workspace`, and `windows` modes |
| `sandbox_max_processes` | `int` | `10` | Max active child processes — Windows Job Object only |
| `sandbox_workspace` | `str \| None` | `None` (→ cwd) | Workspace root path enforced by `workspace` and `windows` modes |
| `sandbox_use_low_integrity` | `bool` | `True` | Windows: run subprocess at Low integrity level |

### Shell tool wiring

The sandbox is applied to **all shell commands** — both the built-in `bash` tool and any commands spawned by subprocesses. The `bash` tool handler delegates execution to `sandbox.execute()`, which applies the platform-appropriate isolation before the process is spawned.

This means `sandbox="local"` is the only mode that provides no isolation. All other modes enforce their restrictions even for one-liner `bash` tool calls.

## Architecture

The safety system has four components:

- **`agent/safety/policy.py`** — `SafetyPolicy` evaluates tool calls against `PolicyConfig` rules, returning ALLOW/DENY/ASK
- **`agent/safety/permissions.py`** — `PermissionManager` handles ASK decisions by calling the approval callback and caching APPROVED_ALWAYS results
- **`agent/safety/sandbox.py`** — `LocalSandbox`, `SubprocessSandbox`, `WorkspaceSandbox`, and `WindowsSubprocessSandbox` control how shell commands are actually executed
- **`agent/tools/builtin/shell.py`** — the `bash` tool handler delegates to the configured sandbox via a closure injected at registration time

These are composed by `ToolExecutor` (`agent/tools/execution.py`), which is the single entry point for all tool execution in the agent loop.
