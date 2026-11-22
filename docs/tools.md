# Built-in Tools Reference

Aar ships with seven core built-in tools, plus `spawn_agent` (gated by
`subagents.enabled` rather than `enabled_builtins`) and `acp_terminal`, which is
registered only by the ACP transport (`agent.transports.acp.stdio`).
The seven core tools are opt-in via `tools.enabled_builtins` in `config.json`.
Tools are grouped by their primary purpose.

## Filesystem tools

Source: `agent/tools/builtin/filesystem.py`

### `read_file`

Read a file and return its contents with line numbers.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | string | yes | — | File path (relative or absolute) |
| `start_line` | integer | no | 0 | First line to return (1-based, inclusive). 0 = start of file. |
| `end_line` | integer | no | 0 | Last line to return (1-based, inclusive). 0 = end of file. |

**Side effects:** READ

**Large file behaviour:** When no line range is specified and the file exceeds
500 lines, `read_file` returns a summary with the total line count and a 30-line
preview instead of dumping the entire file. This prevents accidental context
flooding. Use `start_line` / `end_line` to read the specific section you need.

### `write_file`

Write content to a file, creating parent directories as needed.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | yes | File path to write to |
| `content` | string | yes | Content to write |

**Side effects:** WRITE

### `edit_file`

Replace an exact string in a file. The target string must appear exactly once.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | yes | File path to edit |
| `old_string` | string | yes | Exact string to find (must be unique) |
| `new_string` | string | yes | Replacement string |

**Side effects:** WRITE

### `list_directory`

List files and directories at a given path with types and sizes.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | string | no | `.` | Directory path |

**Side effects:** READ

---

## Shell tools

Source: `agent/tools/builtin/shell.py`

### `bash`

Execute a shell command and return stdout + stderr. Routed through the
sandbox when one is configured.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `command` | string | yes | — | Shell command to execute |
| `timeout` | integer | no | 120 | Timeout in seconds |

**Side effects:** EXECUTE

On Windows `bash -c` needs WSL (or Git Bash). On a Windows PC that has
neither, `powershell` is registered in its place (see below), so a config
written for a WSL machine still gets a working shell.

In `windows` sandbox mode `bash` runs at Low integrity through the same helper
as `powershell`; if the helper cannot lower its token the command is not run
(exit code 1, `aar sandbox: could not lower the integrity level`) — see
[safety.md](safety.md#windows--windows-job-objects--low-integrity-recommended-for-windows).

### `powershell`

Source: `agent/tools/builtin/powershell.py`

Run a PowerShell command or script natively on Windows — no WSL needed. A
separate tool rather than a mode of `bash`: a model handed a tool named `bash`
writes bash, so the description tells it to use PowerShell syntax
(`Get-ChildItem`, `Select-String`, `$env:NAME`; on Windows PowerShell 5.1 also
"no `&&`").

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `command` | string | yes | — | PowerShell command or script |
| `timeout` | integer | no | 120 | Timeout in seconds (`tools.bash_default_timeout`, clamped to `tools.command_timeout`) |

**Side effects:** EXECUTE — the same approval, read-only, deny-list and
`allowed_paths` handling as `bash`; ACP tool kind `execute`.

- **Executable:** `tools.powershell.executable` — `"auto"` (default) picks
  `pwsh` (PowerShell 7) if it is on `PATH`, else Windows PowerShell 5.1
  (`powershell.exe`). `"auto"` resolves to nothing off Windows, so the tool
  only appears on Linux/macOS when an executable is set explicitly.
- **Invocation:** `<exe> -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass
  -OutputFormat Text -Command <bootstrap>`, with the script on stdin. The
  execution policy override is process-scoped. The bootstrap reads stdin as
  UTF-8 and sets `[Console]::OutputEncoding` / `$OutputEncoding` to UTF-8, so
  umlauts survive Windows PowerShell's OEM code page in both directions. No
  `-EncodedCommand`, so there is no 32 KB command-line limit.
- **Process:** started directly (no `cmd.exe`), stdin never inherited,
  `CREATE_NO_WINDOW` so no console window flashes from the desktop widget. It
  starts in the workspace (cwd) and gets the same restricted environment as
  `bash`, plus a few non-secret Windows folder names (`APPDATA`,
  `LOCALAPPDATA`, `ProgramFiles`, `COMPUTERNAME`, …).
- **Result:** stdout, then `STDERR:` and `Exit code: N` like `bash`. The exit
  code follows the last statement (`0` if it succeeded, else the last native
  `$LASTEXITCODE`, else `1`; `exit N` wins). Output is formatted at 4096
  columns and right-trimmed, CRLF becomes LF, and `max_output_chars` truncates
  it like any tool result.
- **Timeout:** the whole process tree is killed (`taskkill /T`, and the Job
  Object in `windows` mode).
- **Sandbox modes:** registered under `local` and `windows` (Job Object + Low
  Integrity, the PowerShell process included). Never under `wsl` or `linux` —
  a host process would escape the sandbox; a config that enables only
  `powershell` gets `bash` (the sandbox's own shell) there instead.

Which shell tools are registered:

| Host / mode | `enabled_builtins` | Registered |
|---|---|---|
| Linux, macOS | `bash` | `bash` (unchanged) |
| Windows with WSL or Git Bash, `local` / `windows` | `bash` | `bash` (unchanged) |
| Windows without bash, `local` / `windows` | `bash` | `powershell` (`tools.powershell.fallback_for_bash`) |
| Windows, `local` / `windows` | `bash`, `powershell` | both (bash only if it exists) |
| any, `wsl` / `linux` | `bash` and/or `powershell` | `bash` |

A fresh config (`aar init`, or no `tools` section) enables `bash` — except on
a Windows PC without bash, where the default list has `powershell` instead.

### `acp_terminal`

Source: `agent/tools/builtin/acp_terminal.py` — registered only by the ACP
transport, when the editor advertises terminal support. Runs the command in
the editor's terminal (the user's own environment) instead of a local
subprocess; parameters `command`, `args`, `cwd`, `env`, `timeout` (default
60 s). Its prompt guidelines name the shell tools actually registered — "prefer
acp_terminal over bash", "… over powershell" or "… over bash and powershell" —
with the reason for each, and drop the comparison when neither is registered.
Repeated environment-shaped failures of `bash` or `powershell` trigger a
one-shot hint toward it (`guardrails.bash_failure_threshold`, see
[agent_loop.md](agent_loop.md#5-shell-bash--powershell--acp_terminal-pivot-hint)).
See [acp.md](acp.md#acp-terminal-tool).

---

## Search tools

Source: `agent/tools/builtin/search.py`

### `grep`

Search file contents using a regex pattern. Returns matching lines with file
paths and line numbers. Searches the working directory recursively, automatically
skipping hidden directories (`.git`, `.hg`, etc.), `__pycache__`, `node_modules`,
`venv`, `dist`, `build`, and other generated paths. Files larger than 2 MB are
also skipped.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `regex` | string | yes | — | Python `re` regex pattern |
| `include_pattern` | string | no | `""` | Glob to restrict which files are searched (e.g. `**/*.py`) |
| `case_sensitive` | boolean | no | `false` | Whether the match is case-sensitive |
| `max_results` | integer | no | 50 | Maximum matching lines to return |

**Side effects:** READ

**Why use `grep` instead of `bash grep`?**

- **No approval needed** — `grep` has side effect READ (auto-approved), while `bash` has EXECUTE (may require approval).
- **Paginated output** — results are capped at `max_results`, preventing context flooding.
- **Structured** — returns `path:line:content` format consistently.
- **Cross-platform** — works on Windows without WSL/bash.

### `find_files`

Find files by glob pattern. Returns file paths relative to the working directory.
Applies the same directory-skipping rules as `grep`.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `glob_pattern` | string | yes | — | Glob pattern (e.g. `**/*.py`, `*.md`, `src/**/*.ts`) |
| `max_results` | integer | no | 200 | Maximum file paths to return |

**Side effects:** READ

---

## Sub-agent tool

Source: `agent/tools/builtin/subagent.py`

### `spawn_agent`

Run a nested agent to completion and return its final message. Registered only when
`subagents.enabled` is true, at least one profile is declared, and the depth budget is
not exhausted — a leaf agent has no `spawn_agent` to call, which is the recursion guard.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `agent_name` | string (enum) | yes | — | Which configured profile to run; the enum is built from `subagents.agents` |
| `task` | string | yes | — | The complete task — the sub-agent cannot see the parent conversation |

**Side effects:** EXECUTE

**Timeout:** the profile's `timeout` is set as the tool's `timeout_s`, so a long child
run is not clipped by `tools.command_timeout`.

Extension tools are a separate list (`extension_tools`): an installed extension
registers into every agent in the process, sub-agents included, so a single-purpose
profile has to name the ones it wants.

Everything else about the child — tools, provider, system prompt, step budget — comes
from the profile in `config.json`, never from the model. The child inherits the parent's
`safety` block and approval callback verbatim, and its built-ins are intersected with
the parent's, so a sub-agent is never more capable than the agent that spawned it.

The tool returns the child's final message plus a one-line footer with the elapsed time
and the child's session id. Errors (unknown profile, timeout, child failure) come back
as error strings rather than exceptions, so the parent can recover and try something
else.

See [Configuration — Sub-agents](configuration.md#sub-agents-spawn_agent) for the key
reference and worked profiles.

---

## Tool selection guide

For efficient codebase navigation, prefer specialised tools over `bash`:

| Task | Recommended tool | Why |
|------|-----------------|-----|
| Find a function definition | `grep` | Paginated, READ-only, cross-platform |
| Locate a config file | `find_files` | Structured results, no shell needed |
| Read a specific function | `read_file` with line range | Avoids loading entire file into context |
| Install a package | `bash` / `powershell` | Needs shell execution |
| Run tests | `bash` / `powershell` | Needs shell execution |
| View directory structure | `list_directory` | Cleaner than `ls` output |
| Survey a large unfamiliar area of the codebase | `spawn_agent` | Keeps dozens of intermediate reads out of the main context; you get back only the conclusion |

## Tool-aware system prompt

Every built-in tool carries two optional metadata fields on its `ToolSpec`:

| Field | Type | Purpose |
|-------|------|---------|
| `prompt_snippet` | `str` | One-line summary shown in the system prompt's "Available tools" section |
| `prompt_guidelines` | `list[str]` | Conditional guidelines injected when this tool is active |

A third field, `timeout_s`, overrides the executor's shared `tools.command_timeout` for
one tool. Set it on tools whose work legitimately runs for minutes — a model render, a
sub-agent — instead of raising the cap for every tool in the process. `None` (the
default) means "use `command_timeout`"; `command_timeout` itself may be `0` to disable
the outer guard entirely.

When any registered tool has a non-empty `prompt_snippet`, the system prompt
automatically includes an **Available tools** section between the base runtime
facts and the rules layers. This gives the LLM a quick-reference overview of
what tools exist before it parses the full JSON tool schemas.

Example output in the assembled system prompt:

```
Available tools:
- read_file: Read file contents (supports line ranges; large files return a preview)
- write_file: Create or overwrite a file
- edit_file: Replace an exact unique string in a file
- list_directory: List files and directories at a path
- bash: Execute a shell command (returns stdout, stderr, exit code)
- grep: Search file contents with regex
- find_files: Find files by glob pattern

Guidelines:
- Use grep to search file contents (symbols, patterns); use find_files for path/filename searches.
```

### Adding prompt metadata to custom tools

When registering your own tools, set `prompt_snippet` and optionally
`prompt_guidelines` on the `ToolSpec` to include them in the system prompt:

```python
from agent.tools.schema import SideEffect, ToolSpec

agent.registry.add(ToolSpec(
    name="fetch_url",
    description="Fetch the contents of a URL and return the body as text.",
    prompt_snippet="Fetch a URL and return its contents",
    prompt_guidelines=["Prefer fetch_url over bash curl for simple HTTP GETs."],
    input_schema={...},
    side_effects=[SideEffect.NETWORK],
    handler=my_handler,
))
```

The system prompt is rebuilt automatically whenever tools are registered
(including after extensions load), so extension tools with snippets are
included too.

Run `aar prompt` to see the assembled system prompt with the tools section.

---

## Configuration

In `config.json`:

```json
{
  "tools": {
    "enabled_builtins": [
      "read_file", "write_file", "edit_file", "list_directory",
      "bash", "grep", "find_files"
    ],
    "bash_default_timeout": 120,
    "command_timeout": 300,
    "max_output_chars": 50000,
    "powershell": {
      "executable": "auto",
      "fallback_for_bash": true
    }
  }
}
```

On Windows, list `"powershell"` (instead of, or next to, `"bash"`) to get the
native shell; see [`powershell`](#powershell).

To disable a tool, remove it from `enabled_builtins`. For example, to create a
read-only agent, keep only `["read_file", "list_directory", "grep", "find_files"]`.

`spawn_agent` is not listed there — it is gated by its own `subagents` block, because
what it can do is defined by that config rather than by the tool itself:

```json
{
  "subagents": {
    "enabled": true,
    "agents": {
      "researcher": {
        "description": "Reads the codebase and answers a question about it",
        "tools": ["read_file", "grep", "find_files"],
        "max_steps": 20
      }
    }
  }
}
```

## Skipped directories

Both `grep` and `find_files` automatically skip these directories:

`.git`, `.hg`, `.svn`, `__pycache__`, `node_modules`, `.venv`, `venv`,
`.tox`, `.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `dist`, `build`,
`.eggs`, `*.egg-info`

Any directory whose name starts with `.` is also skipped.
