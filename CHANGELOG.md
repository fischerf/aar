# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- **Skills are auto-readable** — discovered skills (under `~/.aar/skills/`,
  `.agent/skills/`, or `skills_dirs`) now work even when `allowed_paths`
  restricts file tools to the workspace. The agent adds each skill's directory
  to a new read-only policy allowlist (`PolicyConfig.read_only_paths`), so the
  model can `read_file` a skill's instructions without a manual config change.
  Writes to skill files stay denied, and `denied_paths` still takes precedence
  (a credential file under a skills dir is never exposed). New helper
  `agent.core.skills.skill_read_paths`.
- **Extension UI panels** — extensions can register a transport-agnostic
  panel (`UIPanel`: a `UINode` tree + `UIAction` list) via
  `api.register_panel()`. The fixed TUI renders it in the right column behind
  `ctrl+b` (`ExtensionPanel` + `ConfirmModal`; action keys only while the
  panel has focus, destructive actions confirmed, mutating actions refused
  while the agent runs) and re-renders the transcript after an action or
  extension slash command that rewrote `Session.events`. The ACP stdio agent
  exposes the same data through `_aar/panel_list`, `_aar/panel_snapshot`,
  `_aar/panel_action` and pushes `_aar/panel_changed`; the HTTP/SSE transport
  serves them as `GET /sessions/{id}/panels`, `GET /sessions/{id}/panels/{name}`,
  `POST /sessions/{id}/panels/{name}/actions/{action}` plus a `panel_changed`
  SSE event. First consumer: `aar-ext-shadow-branching` 0.3.0.
- **ACP HTTP keeps one `Agent` per session** — extensions (hooks, tools,
  prompt additions, panels) now load once per `session_id` and keep their
  state across runs instead of being rebuilt and discarded on every run.

### Changed
- Hiding the thinking panel (`ctrl+k`) no longer collapses `#right-col` while
  an extension panel is still visible in it.

### Fixed
- ACP HTTP `GET /runs/{run_id}/events` returned 404 for every run — the
  generic `GET /runs/{run_id}` branch matched first and rejected the path.

### Fixed

---

## [0.4.0] - 2026-06-10


### Added

#### Core / Prompting
- **Skills** — lazy-loaded instruction modules discovered from `~/.aar/skills/`
  and `.agent/skills/`. Each skill is a `.md` file with YAML frontmatter; the
  loader validates them and renders an `<available_skills>` section into the
  system prompt. New module `agent/core/skills.py`.
- **Tool-aware system prompt** — `ToolSpec` gained `prompt_snippet: str` and
  `prompt_guidelines: list[str]`. The registry harvests both
  (`get_prompt_snippets()`, `get_prompt_guidelines()`) and `build_system_prompt`
  inserts an "Available tools" + "Guidelines" layer between the base prompt and
  the rules. `_rebuild_system_prompt()` is called after built-ins and again
  after extension tools register. All seven built-ins ship snippets; `grep`
  carries guidelines distinguishing it from `find_files`.
- **LLM-based context compaction** — new `agent/core/compaction/` package with
  token estimation, cut-point detection, structured summarization, and
  file-operation tracking. New `"compact"` context strategy keeps the first +
  last N messages and inserts a summary marker for dropped middle turns.
- **Two new built-in tools**: `grep` (regex content search) and `find_files`
  (glob path search); both promoted to ACP.
- **`aar prompt --layers`** — shows ordered prompt sources with file paths,
  character counts, and skipped files.

#### Providers
- **Multi-provider configuration** — all providers now live in one config
  file; `cfg.resolve_provider()` returns the active one. The `/model` slash
  command lists, shows, and switches the active provider mid-session
  (`/model`, `/model <key>`, `/model <vendor>/<model>` for ad-hoc switches).
- **`ProviderSwitchEvent`** — typed Pydantic event emitted on every switch;
  surfaced in CLI + TUI transports.
- **Capability mismatch warning** — switching to a provider that lacks tools
  or vision support raises a structured warning before proceeding.
- **Token / cost tallies persisted** — `session_store` saves and restores the
  running totals so resumed sessions stay cost-aware.
- **Configurable timeout precedence** — explicit `timeout` overrides
  `read_timeout`; `read_timeout=null` disables the read timeout entirely
  (useful for slow local models).

#### ACP / Zed
- **`agent-client-protocol` 0.10.0** support — new `message_id` field on
  prompts, `additional_directories` on `session/new`.
- **Extension slash commands** are now populated correctly over ACP.
- **MCP server bridge** — stdio + HTTP MCP servers passed in `session/new` are
  started and their tools registered for the lifetime of that session.

#### TUI
- **Context bar** in the fixed TUI header: `ctx: ████████░░░░ 4.1k/8.2k`,
  reflecting live token usage; visible in all modes (chat / log / thinking).
- **Queued prompts** — typing while the agent is busy queues prompts which
  auto-dispatch when the loop becomes idle. New transport-agnostic
  `agent/transports/prompt_queue.py`.
- **Companion** — kaomoji digital companion widget in the fixed-TUI header;
  progress saved as session metadata. Decoupled from `compact()` via the
  `on_prune` hook.
- **Status bar refresh** on `/model` provider switch.

#### Extensions
- **Plugin extension system** — new `agent/extensions/` package (`api`,
  `loader`, `manager`, `contrib/`). Documented in `docs/extensions.md`.
  `aar init` now scaffolds an example extension configuration.
- **`aar install <package>`** CLI command — installs an extension from PyPI
  or a local path via `pip install`.
- **`aar extensions list` / `aar extensions inspect <name>`** — inspect what
  extensions are discovered and what they register (events, tools,
  commands).
- **Pipeline transforms in `fire_event()`** — handlers can transform events;
  results are chained sequentially through the pipeline.
- **Async handler errors are logged** instead of silently swallowed by
  fire-and-forget tasks.

#### Dev / CI
- **VSCode integration** — `.vscode/` configuration + launch profiles.
- **`scripts/analyze_tokens.py`** — offline analysis of a session's token
  usage.
- **Sonnet 4.6 benchmark** added under `scripts/benchmarks/`.

### Changed

- **`/fork` slash command renamed to `/branch`.**
- **Sample config consolidation** — obsolete provider-specific config files
  removed; all providers sit in one config.
- **Provider config isolation** — added a guard that prevents general
  provider config from being forwarded to specific provider constructors.
- **Rate-limit resilience** — provider runner adds jitter to retry backoff
  and gains a structured `RateLimited` recovery path (Fix A + B in
  `agent/core/provider_runner.py`).
- **Rules / system prompt** — added a parallelism nudge encouraging the model
  to batch independent writes into a single response; `rules.md` trimmed to
  the essentials.
- **ACP session init** — small delay added after session creation so editors
  have time to register tool / command lists before the first prompt.
- **Large-file handling** — improved truncation, summarization, and read
  budgets when tools encounter very large files.
- **`aar init`** — now copies `rules.md` and extension configuration
  examples; recognises `wsl_user` and `restrict_to_workspace` profile fields.

### Fixed

- **Zed launcher scripts** — `scripts/zed/launch.sh` and `launch.cmd` no
  longer attempt `pip install aar-agent>=X.Y.Z` from PyPI (the package is
  not yet published, so the install silently 404'd on first run for any
  user who didn't already have `aar` on `$PATH`). The scripts now hard-error
  with a clear message pointing at the supported source install
  (`pip install git+https://github.com/fischerf/aar.git@v0.4.0`). See
  `docs/pypi-release.md` for the plan to re-enable the PyPI fallback once
  the package is published.
- **Truncated tool calls under `max_tokens`** — loop detects
  `stop_reason="tool_use"` + `output_tokens` at cap + unparsable JSON and
  routes the call through the existing `max_tokens` recovery path. Previously
  the broken tool call was forwarded to the dispatcher, producing repeated
  `invalid_arguments` errors and silently burning the token budget. New
  helper `detect_truncated_tool_call` is provider-agnostic and reuses
  `guardrails.max_tokens_recoveries`; once recoveries are exhausted the loop
  aborts with a clear error naming the offending tool.
- **MCP stdio cancel-scope mismatch** — `MCPClient.__aenter__` /
  `__aexit__` now run in the same `asyncio.Task` (previously `__aexit__`
  could fire from a different RPC dispatcher task or the async-generator GC,
  triggering `Attempted to exit cancel scope in a different task` on shutdown
  or session close).
- **Write-approval deadlock** — batched `write_file` calls under
  `require_approval_for_writes: true` no longer race past `is_auto_approved()`
  and block five threads on `console.input()` simultaneously. Approvals are
  now serialised through the policy engine.
- **`aar_ext_inspect` tests** — skipped cleanly when the extension package
  isn't installed, instead of erroring on import.
- **Large-file edits** — assorted fixes around partial-read truncation and
  off-by-one line markers.
- **Misc** — LF line-ending normalisation in repo, several lint fixes,
  documentation gaps closed.

---

## [0.3.2] - 2026-04-22

### Added

#### Providers
- **Gemini provider** - Google Gemini Pro and Flash support via the official `google-genai` SDK
  and a custom HTTP backend (`aar/providers/gemini.py`); documented in `docs/providers_gemini.md`.

#### ACP / Zed Integration
- **Official ACP SDK transport** - `aar acp` now uses the `agent-client-protocol` Python SDK for
  Zed stdio communication; HTTP/SSE mode remains available via `aar acp --http`.
- **Full session lifecycle** - `load_session`, `list_sessions`, `close_session`, `fork_session`,
  `resume_session`, and `set_mode` / `set_config_option` implemented in the ACP stdio transport.
- **Session mode and config discovery** - `new_session` and `load_session` return
  `modes=SessionModeState` and `config_options` derived from `SafetyConfig` (auto / review /
  read-only; `auto_approve_writes`, `auto_approve_execute`, `read_only` toggles).
- **Thinking and tool-call event streaming** - `ReasoningBlock` emits `AgentThoughtChunk`;
  `ToolCall` emits `ToolCallStart`; `ToolResult` emits `ToolCallProgress`.
- **`@`-mention context support** - `_extract_text` handles `ResourceContentBlock` (URI links)
  and `EmbeddedResourceContentBlock` with `TextResourceContents`.
- **`acp_terminal` built-in tool** - registers only when the client advertises
  `ClientCapabilities(terminal=True)` during `initialize`.
- **Approval process for ACP** - tool calls can be approved or rejected from the editor UI;
  `acp_approval_timeout` config field (default: wait forever, validated against neg/NaN/inf/bool).
- **Slash commands** (`/status`, `/tools`, `/policy`) available in Zed and returned via
  `AvailableCommandsUpdate` on session open.
- **MCP server bridge in ACP** - stdio and HTTP MCP servers passed in `session/new` are started
  and their tools registered for the lifetime of that session.
- **Plan update notifications** - ACP clients receive live plan/step updates during tool execution.
- **SSE byte framing** - `data: <json>\\n\\n` framing verified by new wire-level tests.
- **VSCode integration** - `.vscode/` configuration and launch profiles for local development.

#### Sandbox
- **Docker sandbox** - run agent tools inside an isolated Docker container.
- **Linux Landlock sandbox** - process isolation using Linux kernel >= 5.13 Landlock LSM.
- **Windows Job Object sandbox** - process isolation using Windows Job Objects.
- **Distro profiles** - predefined WSL distro setup profiles shipped with the package under
  `agent/data/distros/`; `aar sandbox setup` reads them automatically.
- **`aar sandbox status`** - new subcommand to inspect config and live distro state.

#### TUI
- **File picker** - `@` in the fixed TUI input opens a modal file browser.
- **Log viewer** - `aar tui --fixed` now includes a dedicated log viewer panel.
- **`think`/channel tag handling** - inline `<think>` and channel tags parsed in the input stream.

#### Core / CLI
- **`aar prompt --layers`** - shows ordered prompt sources with file paths, character counts,
  and skipped files.
- **Configurable provider timeout** - `provider_timeout` field in config.
- **Configurable command timeout** - `command_timeout` for bash/shell tool calls (raised defaults).
- **Budget proximity warning** - core loop emits a warning when approaching the token/cost budget.
- **Guardrails** - configurable guardrail rules (Opus-style) for autonomous loop safety.
- **Search directories for prompt extensions** - additional system prompt directories configurable.
- **Misconfiguration warnings** - startup checks warn on likely config errors.
- **`jsonschema`** added as a core dependency.

### Changed

- **ACP transport refactored** - split into `agent/transports/acp/stdio.py`,
  `agent/transports/acp/http.py`, and `agent/transports/acp/common.py`.
- **Core loop refactored** - cleaner separation between run logic and event dispatch.
- **Sandbox modes reworked** - unified config model covering WSL, Docker, Landlock, Job Object.
- **Agent timeout** - default changed to infinite (no hard cutoff); configurable per-session.
- **WSL setup timeout** - raised to 600 s to accommodate large package installs.
- **ToolResult error prefixes** - unified format `Error [<category>]: ...` across all tools.
- **`Provider.stream()` fallback** - replays text, reasoning, and tool calls with terminal
  metadata when the underlying stream errors mid-response.
- **Path normalization** - `_normalize_path` handles UNC paths, lowercase drive letters, and
  `.`/`..` collapse on both Linux and Windows.
- **Workspace escape guard** - `cwd` is validated to stay inside the configured workspace in
  `WslDistroSandbox.execute`.
- **System prompt for Alpine WSL** - expanded with Alpine-specific shell idioms.
- **Autonomous loop** - enhanced step sequencing and recovery logic.
- **Dependencies updated** - `pydantic`, `httpx`, `rich`, `textual`, `anthropic`, `openai`,
  `google-genai`, `mcp`, `agent-client-protocol` all updated to latest compatible versions.

### Fixed

- **ACP session load/resume** - `load_session` was silently no-op; now correctly restores
  persisted sessions and replays message history to the client before resolving.
- **ACP session listing** - `list_sessions` reads all `.jsonl` files and returns `SessionInfo`
  with title derived from the first assistant message.
- **ACP unknown session on `prompt`** - creates a fresh session instead of crashing.
- **Stream chunk finalisation** - `StreamChunk(finished=True)` now always fires even when the
  stream raises mid-way (wrapped in `try/finally`).
- **Safety/approval edge cases** - fixed races and missing approval callbacks in the policy engine.
- **ACP concurrent prompt rejection** - a second `prompt` on the same session while one is
  in-flight is now correctly rejected with an error response.
- **Keybinds** - external keybind configuration removed (caused setup issues); bindings are now
  defined in code via `agent/transports/keybinds.py`.

---

## [0.3.1] - 2026-04-11

Initial public release with Anthropic, OpenAI, Ollama, and generic provider support; Rich TUI;
Textual full-screen TUI; web API (ASGI/SSE); basic WSL sandbox; JSONL session persistence;
MCP bridge; token budget and cost tracking.

---

## [0.3.0] - 2026-04-06

Internal release.

---

## [0.2.1] - 2026-03-28

Internal release.

---

## [0.2.0] - 2026-03-20

Internal release.

---

[0.4.0]: https://github.com/fischerf/aar/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/fischerf/aar/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/fischerf/aar/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/fischerf/aar/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/fischerf/aar/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/fischerf/aar/releases/tag/v0.2.0