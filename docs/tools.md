# Built-in Tools Reference

Aar ships with seven built-in tools. All are opt-in via `tools.enabled_builtins`
in `config.json`. Tools are grouped by their primary purpose.

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
500 lines, `read_file` returns a summary with the total line count and a 50-line
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

## Tool selection guide

For efficient codebase navigation, prefer specialised tools over `bash`:

| Task | Recommended tool | Why |
|------|-----------------|-----|
| Find a function definition | `grep` | Paginated, READ-only, cross-platform |
| Locate a config file | `find_files` | Structured results, no shell needed |
| Read a specific function | `read_file` with line range | Avoids loading entire file into context |
| Install a package | `bash` | Needs shell execution |
| Run tests | `bash` | Needs shell execution |
| View directory structure | `list_directory` | Cleaner than `ls` output |

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
    "max_output_chars": 50000
  }
}
```

To disable a tool, remove it from `enabled_builtins`. For example, to create a
read-only agent, keep only `["read_file", "list_directory", "grep", "find_files"]`.

## Skipped directories

Both `grep` and `find_files` automatically skip these directories:

`.git`, `.hg`, `.svn`, `__pycache__`, `node_modules`, `.venv`, `venv`,
`.tox`, `.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `dist`, `build`,
`.eggs`, `*.egg-info`

Any directory whose name starts with `.` is also skipped.
