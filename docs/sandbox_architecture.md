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

> **Windows note:** On Windows, `bash` resolves to the WSL interop binary
> (`bash.exe`). This means commands do **not** run as a native Windows process —
> they run inside your **default WSL2 distro** as your WSL user, with full access
> to that distro's filesystem and all `/mnt/<drive>/` mounts. There is no
> Windows-side process isolation whatsoever.

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

> **WSL interop:** Both the normal path and the fallback path call `bash -c <cmd>`.
> On Windows, `bash` is the WSL interop binary, so the shell runs inside the
> user's **default WSL2 distro** — not a native Windows process.  The Low
> Integrity Level token restricts **Windows-host** filesystem writes (user
> profile, Program Files, registry); WSL-internal paths (`/home/`, distro
> packages, etc.) are **not** restricted by the Low IL token.

> **Security note — temp-file TOCTOU:** The Low-IL helper script is written to a
> temp file via `tempfile.mkstemp(prefix="aar_sandbox_")` and then executed as
> `sys.executable`. A concurrent Medium-IL process on the same machine could race
> to overwrite the file between creation and execution, which is a potential
> **privilege-escalation vector**.

> **Security note — permanent `icacls` stamp:** The workspace is stamped
> Low-IL-writable via `icacls` once and **never reverted** — the label persists
> after Aar exits, after reboots, and across uninstalls. Any future Low-IL
> process on the system can write to that directory indefinitely. To undo this
> manually: `icacls <workspace> /setintegritylevel Medium`.

**Protects against:** a rogue command writing to your user profile, SSH keys,
`Program Files`, or the registry.  Memory/process limits keep runaway fork
bombs or OOM loops contained.
**Does not protect against:** reading secrets, network exfiltration, writes to
other Low-IL locations, or writes to WSL-internal paths inside the default
distro.

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

> **Root + `/mnt/` = full Windows host write access:** The agent runs as
> `root` inside the distro with `/mnt/` mounted. This means it can write to
> *any* path the Windows user owns — including `C:\Users\<you>\.ssh`, browser
> profiles, `AppData`, project source trees, and any other file your Windows
> account has permission to modify. This is worse than it might appear: there
> is no mandatory-integrity-level or ACL restriction on `/mnt/` paths from
> inside WSL2. A single `rm -rf /mnt/c/Users/<you>/.ssh` is all it takes.

> **Command-string bypass:** The sandbox checks that the `cwd` argument does
> not escape the workspace, but **the command string itself is never
> inspected**. A command such as `cd /mnt/c/Users && rm -rf .ssh` with the
> default `cwd` executes without any refusal or warning.

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

| Mode | Security rank (Windows) | Writes blocked outside workspace? | Reads blocked? | Memory/process caps | Network restriction | Actual shell on Windows | Recommended for beginners? |
|---|---|---|---|---|---|---|---|
| `linux` | 🥇 1 — kernel Landlock | **yes** (kernel-enforced) | no | ulimit (advisory) | no | native Linux shell | ✅ yes (on Linux) |
| `windows` | 🥈 2 — Low IL + Job Object | mostly (Low IL, Windows ACL) | no | yes (Job Object) | no | WSL interop bash (Low IL token) | ✅ yes (on Windows) |
| `wsl` | 🥉 3 — distro isolation only | **no** (`/mnt/` exposes host, runs as root) | no | no | no | `wsl -d aar-sandbox` dedicated distro | ⚠️ only for clean env, not security |
| `local` | ❌ 4 — no sandbox | no | no | no | no | WSL interop bash (default distro) | ❌ no |

> **New to Aar? Use `auto` mode** (which maps to `windows` on Windows and
> `linux` on Linux). It gives the strongest OS-enforced isolation available on
> your platform with no extra setup. Set `"sandbox": "auto"` in your
> `config.json`.

**None of the current modes restrict outbound network access.**

---

## On Windows: all shell modes route through WSL

One important reality on Windows that cuts across all modes:

| Mode | Shell process | Distro |
|---|---|---|
| `local` | WSL interop `bash.exe` | your default WSL2 distro |
| `windows` | WSL interop `bash.exe` (Low IL token) | your default WSL2 distro |
| `wsl` | `wsl -d aar-sandbox -- sh -c …` | dedicated `aar-sandbox` distro |

`local` and `windows` use the same WSL interop bash, but `windows` mode
adds genuine restrictions: Low Integrity Level blocks Windows-host writes to
your user profile, Program Files, and registry; a Job Object enforces memory
and process-count caps; and environment variables are filtered to a whitelist.

`wsl` is the only mode that uses a **different, isolated distro** — and that is
its value: clean package installs, resettable state. It does not provide
stronger filesystem security than `windows`; in fact it is weaker on that axis
because the agent runs as root with full `/mnt/` access.

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
- `windows`:
  - Doesn't restrict reads (Low IL is write-side only).
  - The Low-IL helper script is written to a temp file with a predictable
    prefix (`aar_sandbox_*.py`). A concurrent Medium-IL process could overwrite
    it before execution — **TOCTOU race / potential privilege escalation**.
  - The `icacls` workspace stamp is **permanent on disk and never reverted** on
    exit. Any future Low-IL process can write to that directory until manually
    undone with `icacls <workspace> /setintegritylevel Medium`.
  - Low IL only restricts Windows-host writes. **WSL-internal paths** inside the
    user's default distro (e.g. `~/.ssh` inside WSL) remain fully writable.
  - Low-IL fallback path is silent if ctypes helpers fail.
- `wsl`:
  - `/mnt/` defeats filesystem isolation for anything on the Windows host —
    the agent runs as **root** and can write to any file your Windows user owns,
    including `.ssh`, browser profiles, and `AppData`.
  - The **command string is never inspected** for path escapes — only the `cwd`
    argument is checked. A command such as `cd /mnt/c/Users && rm -rf .ssh`
    executes without any refusal or warning.
  - No memory or process caps; a runaway command can exhaust system resources.
- `auto` — only as strong as the mode it maps to.

For a threat model that demands more than the above — untrusted agent code,
network egress control, or strict workspace-only filesystem access on Windows —
no current sandbox mode is sufficient; consider running the agent inside an
isolated VM or container outside of Aar.

---

## `allowed_paths` and sandbox modes — what users expect vs. what happens

Users often configure `allowed_paths` in the `safety` block expecting it to
act as a filesystem jail for **all** tool activity including shell commands:

```json
"safety": {
  "allowed_paths": ["<cwd>/**"]
}
```

The reality is more nuanced, and differs significantly between **file tools**
and the **bash tool**.

### File tools (`read_file`, `write_file`, `list_directory`, …)

`allowed_paths` is enforced by the **policy engine** for every file tool call,
regardless of sandbox mode. The policy checks the `path` argument before
execution. If it falls outside the whitelist the call is hard-denied — no
approval dialog, no sandbox bypass. This works correctly in every mode.

### Bash tool (`bash`, shell commands)

The policy engine **cannot inspect which filesystem paths a shell command will
touch at runtime**. The only protections available are:

1. **OS-level write isolation** — the sandbox itself refuses out-of-scope
   writes at the kernel/OS level.
2. **Forced human approval** — every shell call is escalated to `ASK` so a
   human can review it before it runs.

The policy engine uses the sandbox mode to decide which applies:

| Sandbox mode | `allowed_paths` enforced for bash? | Mechanism |
|---|---|---|
| `linux` | ✅ yes | Landlock LSM: kernel refuses writes outside workspace |
| `windows` | ✅ yes (writes only) | Low IL: Windows ACL blocks writes outside workspace |
| `wsl` | ⚠️ forced ASK | No OS write isolation (root + `/mnt/`); every shell call requires approval |
| `local` | ⚠️ forced ASK | No isolation; every shell call requires approval |

> **In plain English:** if you are on Windows with `wsl` or `local` mode and
> you have `allowed_paths` set, Aar will **ask for approval before every shell
> command** rather than silently letting it run. This is intentional — it keeps
> you in the loop when the OS cannot enforce the boundary for you.
>
> If you want shell commands to run without approval prompts *and* be
> restricted to your workspace, use `windows` (or `auto`) mode on Windows, or
> `linux` (or `auto`) mode on Linux.

### Limitation: `allowed_paths` is Windows-path-aware, WSL paths are not checked

`allowed_paths` patterns are matched against the `path` argument passed to file
tools. On Windows, those arguments are Windows paths (`C:\project\file.py`).
When a shell command running inside WSL writes to `/mnt/c/project/file.py`
directly, **no file tool call is made** and therefore no `allowed_paths` check
is triggered. This is a fundamental limitation of the approach: shell-level
writes bypass all policy checks unless the OS sandbox enforces the boundary.

### Summary: what `allowed_paths` actually guarantees

| Tool type | Mode | Guarantee |
|---|---|---|
| File tools | any | ✅ hard deny if path outside whitelist |
| Bash tool | `linux` / `windows` | ✅ OS blocks out-of-scope writes |
| Bash tool | `wsl` / `local` | ⚠️ forced ASK — human review, not OS enforcement |
| Direct WSL writes | any | ❌ not checked — OS sandbox or nothing |

---

## Component map

| Path | Purpose |
|---|---|
| `agent/safety/sandbox.py` | All sandbox classes (`LocalSandbox`, `LinuxSandbox`, `WindowsSubprocessSandbox`, `WslDistroSandbox`) and the `Sandbox` ABC |
| `agent/safety/wsl_manager.py` | WSL2 distro lifecycle helpers (`import_distro`, `unregister_distro`, `download_rootfs`, …) — used by `aar sandbox` CLI |
| `agent/core/config.py` | `SandboxConfig` + per-mode sub-models (`LinuxSandboxConfig`, `WindowsSandboxConfig`, `WslSandboxConfig`, `LocalSandboxConfig`) |
| `agent/tools/execution.py` | `_create_sandbox(config)` — maps `mode` to the concrete `Sandbox` class |
| `agent/transports/cli.py` | `aar sandbox setup / status / reset` commands |