# Themes & Layout

Aar's TUI supports switchable color themes and configurable layout sections. Themes control every color in the interface — panel borders, text styles, badges, the input prompt. Layout controls which sections are visible.

## Quick start

```bash
# launch with a specific theme
aar tui --theme claude
aar tui --theme bladerunner

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
| `bladerunner` | Neon glow — cyberpunk terminal aesthetic with cyan, magenta, and orange. |

## Setting a default theme

In your config file (`~/.aar/config.json`), add a `tui` section:

```json
{
  "provider": { "name": "ollama", "model": "llama3" },
  "tui": {
    "theme": "bladerunner"
  }
}
```

The `--theme` CLI flag overrides the config file, and `/theme` commands override both at runtime.

## Creating a custom theme

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

## Theme resolution order

When Aar looks up a theme name, it checks in order:

1. Built-in themes (`default`, `claude`, `bladerunner`)
2. User themes at `~/.aar/themes/<name>.json`
3. Direct file path (absolute or relative)

## Tips

- Theme switching is instant — it only affects future output. Already-printed text keeps its original colors.
- You can load a theme from any path: `aar tui --theme ./my-themes/experiment.json`
- Partial themes are valid. Override just the fields you care about; the rest use defaults.
- Use hex colors for precise control. Named colors depend on your terminal's palette; hex colors don't.
