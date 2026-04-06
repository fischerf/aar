# Themes & Layout

Aar's TUI supports switchable color themes and configurable layout sections. Themes control every color in the interface — panel borders, text styles, badges, the input prompt, and the fixed status bars. Layout controls which sections are visible.

## Quick start

```bash
# launch with a specific theme
aar tui --theme claude
aar tui --theme decker

# launch in full-screen mode with fixed header/footer bars
aar tui --fixed
aar tui --fixed --theme decker

# switch themes at runtime (inside the TUI)
/theme              # list available themes
/theme claude       # switch to a theme by name
/theme next         # cycle to the next theme
```

## Built-in themes

| Name | Description |
|------|-------------|
| `default` | Classic Aar palette — green, yellow, cyan, red. Matches the original look. |
| `claude` | Warm sand and sage — muted palette inspired by Claude Code. |
| `decker` | Neon glow — cyberpunk terminal aesthetic with cyan, magenta, and orange. |

## Setting a default theme

In your config file (`~/.aar/config.json`), add a `tui` section:

```json
{
  "provider": { "name": "ollama", "model": "llama3" },
  "tui": {
    "theme": "decker"
  }
}
```

The `--theme` CLI flag overrides the config file, and `/theme` commands override both at runtime.

## Creating a custom theme

Run `aar init` to set up `~/.aar/themes/` with:

- **`example.json`** — a full template (copy of the decker theme) ready to rename and edit
- **`theme.schema.json`** — the JSON schema for editor autocompletion

Create a JSON file at `~/.aar/themes/<name>.json`. You only need to include the fields you want to override — everything else falls back to defaults.

### Minimal example

```json
{
  "name": "nord",
  "description": "Nord-inspired arctic palette",
  "assistant": {
    "title_style": "bold #88c0d0",
    "border_style": "#88c0d0"
  },
  "prompt_style": "bold #88c0d0",
  "dim_text": "#4c566a"
}
```

Save this as `~/.aar/themes/nord.json`, then use it:

```bash
aar tui --theme nord
```

### Full example

Every configurable field with its default value:

```json
{
  "name": "mytheme",
  "description": "A complete custom theme",

  "assistant": {
    "title_style": "bold green",
    "border_style": "green",
    "padding": [1, 2]
  },
  "tool_call": {
    "title_style": "bold yellow",
    "border_style": "yellow",
    "padding": [0, 2]
  },
  "tool_result": {
    "title_style": "bold cyan",
    "border_style": "cyan",
    "padding": [0, 2]
  },
  "tool_error": {
    "title_style": "bold red",
    "border_style": "red",
    "padding": [0, 2]
  },
  "reasoning": {
    "title_style": "dim",
    "border_style": "dim",
    "padding": [0, 2]
  },
  "error": {
    "title_style": "bold red",
    "border_style": "red",
    "padding": [0, 2]
  },
  "welcome": {
    "title_style": "bold blue",
    "border_style": "blue",
    "padding": [1, 2]
  },

  "prompt_style": "bold blue",
  "dim_text": "dim",
  "working_style": "dim italic",
  "path_highlight": "bold blue",
  "usage_style": "dim",

  "badges": {
    "read": "dim cyan",
    "write": "yellow",
    "execute": "red",
    "network": "blue",
    "external": "magenta"
  },

  "header": {
    "background": "on #1a1a2e",
    "text_style": "bold white",
    "separator_style": "dim",
    "provider_style": "bold cyan",
    "tokens_style": "dim green",
    "session_style": "dim",
    "state_style": "bold yellow"
  },
  "footer": {
    "background": "on #1a1a2e",
    "text_style": "bold white",
    "separator_style": "dim",
    "step_style": "dim cyan",
    "theme_style": "dim magenta",
    "input_style": "bold blue"
  }
}
```

### Style values

All style fields accept [Rich style strings](https://rich.readthedocs.io/en/latest/style.html). You can combine:

- **Named colors**: `red`, `green`, `cyan`, `magenta`, `blue`, `yellow`, `white`
- **Hex colors**: `#d4a574`, `#00fff7`, `#ff2d95`
- **RGB**: `rgb(200, 100, 50)`
- **Modifiers**: `bold`, `dim`, `italic`, `underline`, `strike`
- **Combinations**: `bold #88c0d0`, `italic dim`, `bold underline red`

### Panel sections

Each panel section (`assistant`, `tool_call`, `tool_result`, `tool_error`, `reasoning`, `error`, `welcome`) has three fields:

| Field | Type | Description |
|-------|------|-------------|
| `title_style` | string | Rich style for the panel title text |
| `border_style` | string | Rich style for the panel border |
| `padding` | `[v, h]` | Vertical and horizontal padding inside the panel |

### Text styles

| Field | What it styles |
|-------|---------------|
| `prompt_style` | The `> ` input prompt |
| `dim_text` | Metadata: session IDs, step counters, hints |
| `working_style` | The "Working..." indicator |
| `path_highlight` | File paths in verbose mode |
| `usage_style` | Token usage counts |

### Badge colors

Badges appear on tool calls in verbose mode (`--verbose`). Each badge field is a single Rich color:

| Field | Badge label |
|-------|-------------|
| `read` | `[read]` |
| `write` | `[write]` |
| `execute` | `[exec]` |
| `network` | `[net]` |
| `external` | `[ext]` |

## Layout configuration

Layout controls which TUI sections are visible. Configure it in `~/.aar/config.json`:

```json
{
  "tui": {
    "theme": "claude",
    "layout": {
      "reasoning": { "visible": false },
      "token_usage": { "visible": false },
      "welcome": { "visible": true }
    }
  }
}
```

### Available sections

| Section | Description | Default |
|---------|-------------|---------|
| `welcome` | Welcome panel shown at startup | visible |
| `status_bar` | Session ID, step count, state | visible |
| `reasoning` | Model thinking/reasoning blocks | visible |
| `assistant` | Assistant message panels | visible |
| `tool_call` | Tool invocation panels | visible |
| `tool_result` | Tool output panels | visible |
| `token_usage` | Token count display | visible |

Each section accepts:

```json
{ "visible": true, "order": 0 }
```

### Extension sections

Extensions can register custom panels. Control their visibility via the `extensions` key:

```json
{
  "tui": {
    "layout": {
      "extensions": {
        "metrics": { "visible": true },
        "custom_panel": { "visible": false }
      }
    }
  }
}
```

## Full-screen mode (fixed bars)

Pass `--fixed` to launch the TUI with a persistent header and footer bar, scrollable body with scrollbars, mouse support, and a proper input widget:

```bash
pip install "aar-agent[tui-fixed]"    # install textual dependency
aar tui --fixed
aar tui --fixed --theme claude --verbose
```

Requires the `tui-fixed` extra (provides [Textual](https://textual.textualize.io)).

### Features

- **Scrollable body** with visual scrollbars
- **Mouse wheel** scrolling (non-blocking — scroll while the LLM is working)
- **Page Up / Page Down** keyboard scrolling
- **Input widget** with full terminal input support (cursor, backspace, selection)
- **Command history** — press **↑ / ↓** to cycle through previous inputs
- **Block selection & copy** — click a block to select it, then **Ctrl+Y** to copy to clipboard
- **Fixed header** showing provider/model, token counts, session ID, agent state, thinking status
- **Fixed footer** showing step count, theme name, and keyboard shortcut hints
- **Configurable layout** — reorder, resize, or hide regions per theme

### Keyboard shortcuts

| Shortcut | Action |
|----------|--------|
| **Escape** | Quit |
| **Ctrl+T** | Cycle to next theme |
| **Ctrl+K** | Toggle thinking/reasoning display |
| **Ctrl+L** | Clear screen and reset counters |
| **Ctrl+Y** | Copy selected (or last) block to clipboard |
| **Page Up / Page Down** | Scroll the conversation body |
| **↑ / ↓** (in input) | Navigate command history |

### Slash commands

All commands from the scrollable TUI also work in fixed mode:

| Command | Action |
|---------|--------|
| `/quit`, `/exit`, `/q` | Quit |
| `/status` | Show session info |
| `/tools` | List available tools |
| `/policy` | Show safety policy |
| `/theme` | List themes |
| `/theme <name>` | Switch theme |
| `/theme next` | Cycle theme |
| `/think` | Toggle thinking display |
| `/copy` | Copy selected block to clipboard |
| `/clear` | Clear screen |

### Layout

```
+------------------------------------------------------------------------+
| Header bar (fixed)                                                      |
| ollama / llama3 | tokens: 1234in/567out | abc… | idle | think:on       |
+────────────────────────────────────────────────────────────────────────+
|                                                                        ┃|
| Scrollable conversation body (with scrollbar)                          ┃|
| (assistant messages, tool calls, results, reasoning, errors)           ┃|
| Click a block to select it for copy                                    ┃|
|                                                                        ┃|
+────────────────────────────────────────────────────────────────────────+
| > type your message... (↑/↓ for history)                                |
+────────────────────────────────────────────────────────────────────────+
| Footer bar (fixed)                                                      |
| step: 5 | theme: claude | Esc quit  Ctrl+T theme  Ctrl+K think  ...    |
+------------------------------------------------------------------------+
```

All `/theme`, `/status`, `/tools`, `/policy`, `/clear`, and `/quit` commands work in fixed mode.

### Header styles

| Field | What it styles |
|-------|---------------|
| `background` | Header bar background color |
| `text_style` | General header text |
| `separator_style` | Horizontal separator line |
| `provider_style` | Provider and model name |
| `tokens_style` | Token count display |
| `session_style` | Session ID |
| `state_style` | Agent state (idle, running, etc.) |

### Footer styles

| Field | What it styles |
|-------|---------------|
| `background` | Footer bar background color |
| `text_style` | General footer text |
| `separator_style` | Horizontal separator line |
| `step_style` | Step counter |
| `theme_style` | Theme name display |
| `input_style` | Input status text |

### Fixed layout configuration

The `fixed_layout` section in a theme controls the full-screen TUI's region order, sizes, colors, and scrollbar appearance. This is the single source of truth for both layout structure and visual styling.

```json
{
  "name": "mytheme",
  "fixed_layout": {
    "regions": [
      { "name": "header", "size": 3 },
      { "name": "body", "size": null },
      { "name": "input", "size": 3 },
      { "name": "footer", "size": 3 }
    ],
    "body_background": "#0e0e0e",
    "input_background": "#111118",
    "scrollbar": {
      "enabled": true,
      "color": "#444444",
      "color_hover": "#666666",
      "color_active": "#888888",
      "background": "#1a1a1a",
      "background_hover": "#222222",
      "background_active": "#222222",
      "size": 2
    }
  }
}
```

#### Regions

Each region has:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Region name: `header`, `body`, `input`, `footer` |
| `size` | int or null | Fixed height in lines. `null` = flexible (fills remaining space) |
| `visible` | bool | Whether to show this region (default: `true`) |

You can reorder regions by changing the array order. For example, to put the footer above the input:

```json
{
  "regions": [
    { "name": "header", "size": 3 },
    { "name": "body" },
    { "name": "footer", "size": 3 },
    { "name": "input", "size": 3 }
  ]
}
```

Or hide the header entirely:

```json
{
  "regions": [
    { "name": "header", "size": 3, "visible": false },
    { "name": "body" },
    { "name": "input", "size": 3 },
    { "name": "footer", "size": 3 }
  ]
}
```

#### Scrollbar

| Field | Type | Description |
|-------|------|-------------|
| `enabled` | bool | Show scrollbar on the body region |
| `color` | string | Scrollbar thumb color |
| `color_hover` | string | Thumb color on hover |
| `color_active` | string | Thumb color while dragging |
| `background` | string | Scrollbar track color |
| `background_hover` | string | Track color on hover |
| `background_active` | string | Track color while dragging |
| `size` | int | Scrollbar width in characters |

Header/footer styles and fixed_layout are all optional — if omitted, the defaults are used.

## Theme resolution order

When Aar looks up a theme name, it checks in order:

1. Built-in themes (`default`, `claude`, `decker`)
2. User themes at `~/.aar/themes/<name>.json`
3. Direct file path (absolute or relative)

## Tips

- Theme switching is instant — it only affects future output. Already-printed text keeps its original colors.
- You can load a theme from any path: `aar tui --theme ./my-themes/experiment.json`
- Partial themes are valid. Override just the fields you care about; the rest use defaults.
- Use hex colors for precise control. Named colors depend on your terminal's palette; hex colors don't.
- Run `aar init` to get a full example theme and JSON schema in `~/.aar/themes/`.
