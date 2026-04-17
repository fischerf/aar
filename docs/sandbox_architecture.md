# Sandbox Architecture

This document shows *how each sandbox mode actually works* — the execution path
from the agent through the operating system, and the real isolation properties
each one offers.  Read [Safety](safety.md) for configuration reference; read
this document to understand **what each mode does and does not protect you
against**.

---

## Execution pipeline (all modes)

```
  +-----------------------+
  | Agent loop            |
  | (agent/core/loop.py)  |
  +-----------+-----------+
              |
              | bash(command="...")      tool call event
              v
  +-----------------------+
  | ToolExecutor          |  policy check (allow/deny/ask)
  | execution.py          |  approval callback (if ASK)
  +-----------+-----------+
              |
              | sandbox.execute(command, cwd, env, timeout)
              v
  +-----------------------+
  | Sandbox (per mode)    |  wraps command + spawns subprocess
  | sandbox.py            |
  +-----------+-----------+
              |
              v
        OS / kernel
```

Every sandbox mode returns the same `SandboxResult` shape (stdout, stderr,
exit_code, timed_out), so transports and tools are decoupled from the backend.
The differences are entirely in **how the subprocess is spawned** and **what
restrictions are applied** before/around it.

---

## `local` — no sandbox

```
  agent (Python)
      |
      | asyncio.create_subprocess_exec("bash", "-c", cmd)
      v
   bash -c cmd
      |
      v
   [runs as your user, inherits full env, full filesystem access]
```

| Property | Value |
|---|---|
| Filesystem | **unrestricted** — read/write anywhere the user has permission |
| Network | **unrestricted** |
| Env vars | **full parent env** inherited |
| Resource caps | none |
| Process privileges | your user token |

**Protects against:** nothing. Name it "no sandbox" in your head.
**Use when:** trusted local dev, you want the agent to behave exactly like you
at the terminal.

---

## `linux` — Landlock LSM

Linux kernel ≥ 5.13. The agent's Python process asks the kernel to apply a
filesystem-access ruleset to the next subprocess via `landlock_restrict_self()`
inside a `preexec_fn` (runs after `fork()`, before `exec()`):

```
  agent (Python)
      |
      | fork()
      v
  child process (pre-exec)
      |
      |  prctl(PR_SET_NO_NEW_PRIVS, 1)
      |  landlock_create_ruleset(ALL_V1)
      |  landlock_add_rule(/, READ+EXEC only)
      |  landlock_add_rule(<workspace>, ALL)
      |  landlock_restrict_self()        <-- kernel enforces from here on
      |
      | execve("bash", "-c", cmd)
      v
  bash -c "ulimit -v <cap>; cmd"
      |
      v
  [kernel refuses openat(O_WRONLY) outside <workspace>]
```

| Property | Value |
|---|---|
| Filesystem writes | **kernel-blocked outside `<workspace>`** (Landlock ABI v1) |
| Filesystem reads | unrestricted (Landlock v1 doesn't restrict reads) |
| Network | unrestricted |
| Env vars | restricted to `PATH`, `HOME`, `TERM`, `LANG` |
| Resource caps | `ulimit -v <max_memory_mb>` (virtual memory, advisory) |
| Fallback | kernel < 5.13 → env restriction + ulimit only (warning logged) |

**Protects against:** a rogue command writing to your home directory, SSH keys,
system locations, or anywhere else outside the workspace.  The kernel refuses
the syscall — no process-level tricks can bypass it.
**Does not protect against:** reading secrets (Landlock v1 has no read rule),
network exfiltration, CPU/disk exhaustion.

---

## `windows` — Job Object + Low Integrity Level

Two layered mechanisms. First, a Python helper self-lowers its mandatory
integrity level to *Low* (the same level as IE Protected Mode) before spawning
`bash`. Second, the outer agent creates a kernel Job Object and assigns the
helper PID to it:

```
  agent (Python)
      |
      | 1. icacls <workspace> /setintegritylevel (OI)(CI)Low
      |    (one-time, grants workspace writes at Low IL)
      |
      | 2. asyncio.create_subprocess_exec(python, helper.py, "bash", cmd)
      v
  helper.py  [still at Medium IL]
      |
      | SetTokenInformation(TokenIntegrityLevel, LOW_RID)
      | (self-demote to Low IL)
      v
  helper.py  [now at Low IL]
      |
      | subprocess.run(["bash", "-c", cmd])
      v
  bash -c cmd           [Low IL — inherits from helper]
      |
      v
  [blocked by Windows from writing Medium/High-IL locations;
   workspace explicitly stamped Low-writable, so that works]

  (concurrent) agent:  CreateJobObject + AssignProcessToJobObject(helper_pid)
                       limits: ProcessMemory, ActiveProcessLimit, KILL_ON_JOB_CLOSE
```

| Property | Value |
|---|---|
| Filesystem writes | Low IL **blocks writes** to user profile, Program Files, HKCU registry; workspace is stamped Low-writable |
| Filesystem reads | unrestricted — Low IL does not restrict reads |
| Network | unrestricted |
| Memory cap | Job Object `ProcessMemoryLimit` = `windows.max_memory_mb` |
| Process count cap | Job Object `ActiveProcessLimit` = `windows.max_processes` |
| Cleanup | `KILL_ON_JOB_CLOSE` — orphans killed when agent exits |
| Fallback | Low-IL helper fails → Job Object only (warning logged) |

**Protects against:** a rogue command writing to your user profile, SSH keys,
`Program Files`, or the registry.  Memory/process limits keep runaway fork
bombs or OOM loops contained.
**Does not protect against:** reading secrets, network exfiltration, writes to
other Low-IL locations.

---

## `wsl` — dedicated WSL2 distro

A separate, disposable WSL2 distro (typically Alpine, set up via
`aar sandbox setup`) acts as the execution environment. Commands are routed
directly through the WSL launcher:

```
  agent (Python, Windows host)
      |
      | asyncio.create_subprocess_exec(
      |     "wsl", "-d", "aar-sandbox", "--", "sh", "-c",
      |     "cd /mnt/<workspace> && <cmd>"
      | )
      v
  wsl.exe  (WSL2 launcher)
      |
      v
  /init (aar-sandbox distro)
      |
      v
  sh -c "cd /mnt/... && cmd"        [runs as root inside distro]
      |
      v
  [distro's /etc, /home, /usr etc. are isolated from host and other distros]
  [BUT: /mnt/b, /mnt/c, ... expose the entire Windows filesystem read+write]
```

| Property | Value |
|---|---|
| Distro filesystem (`/etc`, `/usr`, installed packages) | **isolated** from host Windows and user's main WSL2 distro |
| Host Windows filesystem | **fully reachable at `/mnt/<drive>/`** — not sandboxed |
| Network | unrestricted (shared with host adapter) |
| Memory/process caps | none |
| Privileges inside distro | **root** |
| Resettable | `aar sandbox reset` wipes everything installed in the distro |

**Protects against:** polluting the host with `apt install` / `pip install` /
background daemons; interference with the user's main WSL2 distro; accumulated
tool state across sessions (reset wipes it).
**Does not protect against:** anything the command does to `/mnt/c/Users/you/…`
— that path is your real user profile, writable as root.  SSH keys, browser
cookies, project source tree are all reachable.

**Think of it as:** a clean, wipeable *execution environment*, not a filesystem
jail.  Good for "I want Python 3.12 + some pip packages without polluting my
host" — not for "I want the agent to be unable to touch my SSH key".

---

## `auto` — platform-appropriate default

```
  mode == "auto"
      |
      +-- os.name == "nt"               --> windows
      +-- sys.platform.startswith("linux")  --> linux
      +-- otherwise (macOS, BSD, ...)   --> local
```

On macOS there is currently no OS-level sandbox (Landlock is Linux-only,
Apple's sandbox API is not wired up). `auto` falls back to `local`.

---

## Quick reference — what each mode actually secures

| Mode | Writes blocked outside workspace? | Reads blocked? | Memory/process caps | Network restriction | Runs as |
|---|---|---|---|---|---|
| `local`   | no                             | no  | no  | no | your user |
| `linux`   | **yes** (kernel, Landlock)      | no  | Unix memory | no | your user |
| `windows` | mostly (Low IL, Windows ACL)    | no  | yes | no | your user (Low IL) |
| `wsl`     | **no** (`/mnt/` exposes host)   | no  | no  | no | root inside distro |

**None of the current modes restrict outbound network access.**

---

## When to choose which mode

| Situation | Recommended mode |
|---|---|
| Trusted local dev, want the agent to act like you | `local` |
| Linux server / CI, want write isolation | `linux` (or `auto`) |
| Windows workstation, want write isolation + resource caps | `windows` (or `auto`) |
| Windows, need disposable multi-language execution environment | `wsl` |
| macOS | `local` — no OS-level sandbox available |

## Where each mode is weak

- `local` — everywhere.
- `linux` — doesn't restrict reads; doesn't restrict network; Landlock v1
  can't block `chroot` or `ptrace`-based escapes.
- `windows` — doesn't restrict reads (Low IL is write-side only); relies on
  the `icacls` workspace stamp (sticky on disk); Low-IL fallback path is
  silent if ctypes helpers fail.
- `wsl` — `/mnt/` defeats filesystem isolation for anything on the Windows
  host; no memory/process caps; agent runs as root inside the distro.
- `auto` — only as strong as the mode it maps to.

For a threat model that demands more than the above — untrusted agent code,
network egress control, or strict workspace-only filesystem access on Windows —
no current sandbox mode is sufficient; consider running the agent inside an
isolated VM or container outside of Aar.

---

## Component map

| Path | Purpose |
|---|---|
| `agent/safety/sandbox.py` | All sandbox classes (`LocalSandbox`, `LinuxSandbox`, `WindowsSubprocessSandbox`, `WslDistroSandbox`) and the `Sandbox` ABC |
| `agent/safety/wsl_manager.py` | WSL2 distro lifecycle helpers (`import_distro`, `unregister_distro`, `download_rootfs`, …) — used by `aar sandbox` CLI |
| `agent/core/config.py` | `SandboxConfig` + per-mode sub-models (`LinuxSandboxConfig`, `WindowsSandboxConfig`, `WslSandboxConfig`, `LocalSandboxConfig`) |
| `agent/tools/execution.py` | `_create_sandbox(config)` — maps `mode` to the concrete `Sandbox` class |
| `agent/transports/cli.py` | `aar sandbox setup / status / reset` commands |
