# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.3.2]: https://github.com/fischerf/aar/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/fischerf/aar/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/fischerf/aar/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/fischerf/aar/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/fischerf/aar/releases/tag/v0.2.0