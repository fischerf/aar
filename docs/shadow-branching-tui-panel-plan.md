# Implementation plan: extension panels (shadow-branching in `aar tui --fixed` + ACP)

**Source design:** `docs/ideas/shadow-branching-tui-panel-implementation.md`
**Prerequisite (done):** `/done` disarm fix — `docs/issues/shadow-branching-done-fix.md`, shipped in `aar-ext-shadow-branching` 0.2.1
**Targets:** `fischerf/aar` `develop_gemini_fixes` · `aar-ext-shadow-branching` 0.2.1 → 0.3.0

This is the design doc re-checked against the code as of 2026-09-05, with the
corrections folded in, plus the ACP part the design deferred to "later".

## Status (2026-09-05): implemented

| Piece | Where | Tests |
|---|---|---|
| Contract (`UINode`, `UIAction`, `UIInvocation`, `UIPanel`, `run_ui_snapshot`, `run_ui_action`, `register_panel`, `ExtensionManager.panels`) | `agent/extensions/api.py`, `manager.py` | `tests/test_extension_panels.py::TestContract` |
| Fixed TUI: `ExtensionPanel` + `ConfirmModal`, `ctrl+b`, right-col fix, header chip, transcript re-render, refresh on tool result / poll / slash command | `agent/transports/tui_widgets/extension_panel.py`, `tui_fixed.py`, `keybinds.py`, `tui_widgets/bars.py` | `tests/test_extension_panels.py::TestRightCol`, `::TestExtensionPanelWidget` |
| ACP stdio: `_aar/panel_list` / `_aar/panel_snapshot` / `_aar/panel_action` + `_aar/panel_changed` | `agent/transports/acp/stdio.py` (`ext_method`, `_push_panel_changes`) | `tests/test_acp_panels.py` |
| ACP HTTP: one cached `Agent` per session; `GET /sessions/{id}/panels[/{name}]`, `POST …/actions/{action}`, `panel_changed` SSE event | `agent/transports/acp/http.py` | `tests/test_acp_http_panels.py` |
| Plugin 0.3.0: snapshot, seven actions, `files`/`flagged` on checkpoints, change signalling | `aar_ext_shadow_branching/__init__.py` | `tests/test_shadow_branching_panel.py` (23) |
| Docs | `docs/acp.md` §4, `CLAUDE.md`, plugin `README.md` | — |

Deviations from §2–§5 below, decided during implementation:

* The extension marks `changed` from `_sync_metadata` (every state mutation
  ends there) rather than from six call sites; the `SessionStore.save` hook
  goes through a module-level `_panel_events` registry.
* `UIAction.handler` may be sync; the core threads it. The plugin's handlers
  are plain sync wrappers around `_do_undo` / `cmd_*`.
* `ExtensionPanel.invoke` is a `@work(exclusive=True)` worker (needed for
  `push_screen_wait`); after a rebuild the cursor is restored via
  `call_after_refresh` because `Tree` computes node lines lazily.
* Double-click and clickable hint buttons (mockup 5.6) were not built.
* HTTP ACP was covered after all: the transport used to build a throw-away
  `Agent` per run (extensions loaded and discarded each time); it now caches
  one per `session_id`, which is what makes panels — and any stateful
  extension — work there. Extension slash commands are still not parsed over
  HTTP.

---

## 1. Corrections to the design (verified against the code)

| # | Design says | Code says | Consequence |
|---|---|---|---|
| 1 | "factor out the code the slash path already uses after `/undo`" into `reload_session_if_changed()` | No such code. `/undo` reloads `Session.events` **in place** inside the plugin (`reload_session_from_disk`); `tui_fixed.py` never re-renders. `SessionStore.load` appears once (`on_mount`, startup). | Net-new `_rerender_session()`: clear `ChatBody`, replay `self._session.events` through `FixedTUIRenderer.render_event`. Also applied after extension slash commands — fixes the stale transcript after a typed `/undo` today. |
| 2 | Mount `ExtensionPanel` into `#right-col` next to `ThinkingPanel` | `FixedTUIRenderer.toggle_thinking` (`tui_fixed.py:214`) sets `display: none` on **`#right-col` itself** to release the 40-col track | Ctrl+K would hide the shadow panel too. Collapse `#right-col` only when *every* child is hidden (`_sync_right_col`). |
| 3 | `n_back = len(checkpoints) - idx` | `/undo N` / `/branch N` = "N back from the tip". `_do_undo` normalises `0 → 1`. | "To here" on index `idx` is `len - 1 - idx`; `0` must never reach `_do_undo` (undo disabled on the tip node). Mockup 5.4 already assumes the corrected semantics. |
| 4 | Snapshot reads `cp["files"]`, `cp["flagged"]` | Checkpoints are `{"turn", "hash", "tool"}` | Record `files`/`flagged` at commit time; for reconstructed checkpoints compute lazily from `git show --name-only`, cached by SHA. |
| 5 | Module-level `_PANEL = UIPanel(..., status=lambda ctx: … state …)` | `state` is a `register()`-local closure variable | Build the panel inside `register()`. One `changed` event per registration, not per module. |
| 6 | `await self.app.push_screen_wait(ConfirmModal(...))` from `on_key` | Textual 8.2.3: `push_screen_wait` requires a worker context; the codebase uses the callback form (`push_screen(FilePickerModal(...), cb)`) | `_invoke` is a `@work(exclusive=True)` method — gives the worker context *and* keeps git off the event loop. |
| 7 | "ctrl+b" | Free (no other binding uses it) | — |
| 8 | §8 "later: IDE clients over ACP" | SDK 0.10.0 routes client requests `_<name>` → `AarAcpAgent.ext_method(name, params)`; `conn.ext_notification("aar/x", …)` pushes `_aar/x` | Doable now over **stdio** ACP with no SDK changes. **HTTP** ACP loads no extensions at all (documented gap in `acp/http.py`) → out of scope. |

Also load-bearing: extension slash-command handlers run **synchronously on the
Textual event loop** (`tui_fixed.py:1247`), and every `cmd_*` shells out to git.
Panel refreshes on a poll would make this visible, so the contract normalises
handlers through `asyncio.to_thread` for sync callables (`run_ui_action`,
`run_ui_snapshot` in `api.py`).

---

## 2. Contract (`agent/extensions/api.py`)

```python
@dataclass
class UINode:
    id: str; label: str; kind: str = "info"
    children: list[UINode]; expanded: bool = True; style: str = ""
    data: dict[str, Any]
    def to_dict(self) -> dict          # JSON-safe, for ACP
    def find(self, node_id) -> UINode | None

@dataclass
class UIAction:
    id: str; label: str; key: str; kinds: tuple[str, ...]
    handler: Callable[[UIInvocation], str | None | Awaitable[str | None]]
    destructive: bool = False; confirm: str = ""
    inputs: tuple[str, ...] = ()       # extra args the UI collects: "force", "message"
    mutates: bool = True               # refused while the agent is running

@dataclass
class UIInvocation: node: UINode; ctx: Any; args: dict[str, Any]

@dataclass
class UIPanel:
    name: str; title: str
    snapshot: Callable[[Any], UINode | Awaitable[UINode]]
    actions: list[UIAction]
    status: Callable[[Any], str] | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)

async def run_ui_snapshot(panel, ctx) -> UINode     # sync → to_thread
async def run_ui_action(action, invocation) -> str | None
```

`ExtensionAPI.register_panel(panel)`, `ExtensionAPIProtocol.register_panel`
stub, `ExtensionManager.panels -> dict[str, UIPanel]`.

No Textual import anywhere in this layer. Transports that don't know about
panels ignore them; with no panel registered the TUI is unchanged.

---

## 3. Fixed TUI

**`keybinds.py`** — `toggle_panel = KeyBind("ctrl+b", "panel")`; `FooterBar`
shows it.

**`tui_widgets/extension_panel.py`** (new)

* `ExtensionPanel(Vertical)`: title `Static`, `Tree[UINode]`, hint `Static`.
  * `refresh_tree()` — snapshot via `run_ui_snapshot`, rebuild keeping
    expansion + cursor keyed by `UINode.id`, clear `changed`, redraw hints.
  * `on_key` — action keys only while the panel has focus and the selected
    node's kind is in `action.kinds`. `escape` posts `ExtensionPanel.Close`.
  * `_invoke` (`@work(exclusive=True)`): refuse `mutates` actions while the
    agent runs; destructive → `ConfirmModal`; run via `run_ui_action`; write
    the result line to the chat body (same path as slash output); refresh;
    notify the app to re-render the session if the action mutated.
* `ConfirmModal(ModalScreen[dict | None])`: message, `[y]`/`[n]`/`esc`,
  optional `[f] force` toggle and a commit-message `Input`, driven by
  `UIAction.inputs`.

**`tui_fixed.py`**

* `_make_body_split`: one `ExtensionPanel` per `agent._extension_manager.panels`
  mounted in `#right-col`, hidden by default.
* `_sync_right_col()` — used by `toggle_thinking` and `action_toggle_panel`.
* `action_toggle_panel` (ctrl+b): hidden → show + focus; focused → back to
  input; visible-unfocused → focus. `escape` in the panel hides it.
* `_refresh_panels(force)`: called from a 5 s poll worker, after the agent
  worker finishes, after extension slash commands, and from the renderer's
  `ToolResult` branch (via `app.call_later`).
* `HeaderBar.panel_status` rendered by `_HeaderInfoStatic`.
* `_rerender_session()` (correction 1).
* Welcome screen lists `⎇ <title> — ctrl+b`.

---

## 4. ACP (stdio) — `agent/transports/acp/stdio.py`

Custom methods, all namespaced `aar/`; the SDK adds the `_` wire prefix.

| Direction | Method | Params | Result |
|---|---|---|---|
| client → agent | `_aar/panel_list` | `sessionId` | `{panels: [{name, title, status, actions: [{id, label, key, kinds, destructive, confirm, inputs, mutates}]}]}` |
| client → agent | `_aar/panel_snapshot` | `sessionId, panel` | `{root: UINode.to_dict(), status}` |
| client → agent | `_aar/panel_action` | `sessionId, panel, action, nodeId, args?` | `{message, root, status}` |
| agent → client | `_aar/panel_changed` | `sessionId, panel` | (notification) after a prompt or slash command when the panel's `changed` is set |

Errors: unknown method / panel / action / node → `RequestError.invalid_params`
(unknown method → `method_not_found`). Actions run while a prompt is in flight
are rejected with `invalid_params` when `mutates` is set, mirroring the TUI rule.
`session_id` is accepted in both camel and snake case (the SDK sends camel).

HTTP transport: not covered — no extension manager there.

---

## 5. Plugin (`aar-ext-shadow-branching` 0.3.0)

* Checkpoint dict gains `files: int`, `flagged: bool`; `_checkpoint_details(sha)`
  backfills for reconstructed ones (cached).
* `_snapshot(ctx)` (sync, run in a thread by the core): root → base, active
  branch (newest checkpoint first, `●` on the tip, `⚠` when flagged), sibling
  branches collapsed with `(N cp)`, pending-changes info node. After `/done`:
  the inactive node with the "merged via /done" hint.
* Actions: `undo` (destructive, `inputs=("force",)`), `branch`, `switch`,
  `diff` (`mutates=False`), `delete` (destructive; refuses active + base),
  `done` (destructive, `inputs=("message",)`, implies `--yes`), `refresh`
  (`mutates=False`).
* `changed.set()` from `on_tool_result`, `_do_undo`, `cmd_branch`, `cmd_switch`,
  `cmd_done`, and the `SessionStore.save` wrapper (via a module-level
  `_panel_events` registry keyed by session id, like `_active_states`).
* Import of the contract guarded by `try/except ImportError` — commands keep
  working on an older core.

---

## 6. Tests

* `tests/test_extension_panels.py` (core): contract round-trip, `manager.panels`,
  `run_ui_action` with sync + async handlers, tree rebuild keeps cursor and
  expansion, destructive action opens the modal and is cancelled by `n`, keys
  ignored without focus, `#right-col` only collapses when all children hidden,
  ACP `ext_method` round-trips for list/snapshot/action + `panel_changed`
  notification, `mutates` action rejected mid-prompt.
* Plugin `tests/test_shadow_branching.py`: snapshot shape from a temp repo,
  each action via `UIInvocation`, `undo` disabled on the tip, `delete` refuses
  active/base, inactive snapshot after `/done`, `changed` set by a checkpoint.

---

## 7. Order

1. Contract + manager (core, standalone).
2. Plugin snapshot/actions/tests against the contract — no TUI needed.
3. `ExtensionPanel` + `ConfirmModal`.
4. `tui_fixed.py` wiring, right-col fix, session re-render.
5. ACP `ext_method` + notification + docs (`docs/acp.md`).
6. Tests for 3–5, READMEs, CLAUDE.md line.

Cut from this iteration: double-click primary action and clickable hint-strip
buttons (mockup 5.6) — keyboard + row-select covers the workflow, and
click-to-run a destructive action on a tree row is too easy to trigger.
