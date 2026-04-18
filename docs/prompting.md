# Prompting Guide

Practical advice for getting the best results from Aar across different providers, task types, and modalities.

---

## System prompt

The system prompt is the highest-leverage lever you have. Aar assembles it automatically from up to three layers (see [Configuration](../README.md#configurable-system-prompt)), but every layer is just markdown text — write it the same way.

### What to put in the system prompt

| What | Why |
|---|---|
| Role / persona | Keeps tone consistent across all turns |
| Hard constraints | Things the agent must never do ("never delete files outside `src/`") |
| Output format rules | JSON only, no preamble, always wrap code in fences |
| Domain context | Stack, language, framework, conventions |
| Tool guidance | Which tools to prefer, when to ask before acting |

### What not to put there

- Task-specific instructions belong in the user turn, not the system prompt.  
  Bloating the system prompt with per-task detail wastes context and confuses the model.
- Repetitions of the agent's built-in safety rules — they are enforced in code, not by prompt.

### Minimal working example

```python
from agent import AgentConfig

config = AgentConfig(
    system_prompt="""
You are a senior Python engineer working inside this repository.

Rules:
- Use pathlib, not os.path.
- All public functions must have type hints.
- Write pytest tests for every new function; place them in tests/.
- Before editing a file, read it first with read_file.
- Never modify files outside the current working directory.
- When unsure, ask — do not guess and overwrite.
""".strip()
)
```

### Project rules file

The system prompt is assembled from five layers (all optional except Base):

1. **Base** — runtime facts (OS, cwd, shell, sandbox environment) — always included. On Windows with `wsl` sandbox mode the base includes the distro description from `system_prompt_hint` in your distro profile.
2. **Global rules** — `~/.aar/rules.md` — user-wide preferences
3. **Global drop-ins** — `~/.aar/rules.d/*.md` (sorted) — environment-specific additions without editing the main file
4. **Project rules** — `<project_rules_dir>/rules.md` — project instructions checked into git
5. **Project drop-ins** — `<project_rules_dir>/rules.d/*.md` (sorted) — per-contributor or per-machine overrides; can be gitignored

Run `aar prompt --layers` to see the ordered list of all active sources, their file paths, and how many characters each contributes. Missing files are shown as skipped.

For team projects, put rules in `.agent/rules.md` at the repo root and commit it.
Aar picks it up automatically — no code change needed.

```markdown
# Project rules
- This is a FastAPI + SQLAlchemy project.
- Database models live in app/models/; never put SQL in routes.
- All routes must have an integration test in tests/api/.
- Use Alembic for migrations; never edit the DB schema directly.
```

For machine-local or per-contributor additions that shouldn't be committed, drop `.md` files into `.agent/rules.d/` and add the directory to `.gitignore`. Run `aar init` to scaffold both `rules.md` and `rules.d/` for global and project layers.

---

## Writing effective user prompts

### Be specific about the output format

Bad:
> Refactor the auth module.

Good:
> Refactor `app/auth/tokens.py` so that `create_token` and `verify_token` are the only public functions. Keep the existing interface — callers must not change. Add type hints. Do not touch `app/auth/middleware.py`.

### Scope one concern per turn

Multi-step tasks work better when you break them into focused turns within a single session. The agent has full context from prior turns, so you lose nothing by not cramming everything into one prompt.

```python
session = await agent.run("Read src/parser.py and summarise what it does.", session)
session = await agent.run("Now extract the tokeniser into a separate file src/tokeniser.py.", session)
session = await agent.run("Add a pytest test for the tokeniser in tests/test_tokeniser.py.", session)
```

### Anchor the agent to files it should read

If you want the agent to work on specific files, name them. It will `read_file` them before acting.

> Read `src/database.py` and `src/models/user.py`, then add a `get_by_email` method to `UserModel` that queries by email address.

### Give examples inline when the format is non-obvious

> Add a `retry` decorator. It should work like this:
>
> ```python
> @retry(times=3, delay=0.5)
> async def fetch(url: str) -> str: ...
> ```
>
> Raise the original exception after all retries are exhausted.

---

## Multi-turn sessions

Sessions accumulate context across turns. Use this intentionally.

### Exploration → implementation pattern

Start with a read-only exploration turn, then act:

```python
# Turn 1 — explore, no writes
session = await agent.run(
    "Read all files in src/pipeline/ and describe the data flow. Do not modify anything.",
    session,
)

# Turn 2 — act with context
session = await agent.run(
    "Now add an optional `dry_run` flag to the pipeline's run() method.",
    session,
)
```

### Correction turns

The agent's last output is in the session. You can correct it conversationally:

```python
session = await agent.run("Add type hints to all functions in utils.py.", session)
session = await agent.run(
    "The `parse_date` function uses `datetime` but you did not import it. Fix that.",
    session,
)
```

### Keeping sessions lean

Long sessions use more tokens on every turn (the full history is re-sent). For large tasks, either:

- Use `SessionStore.compact(session_id, max_events=N)` to trim old events, or
- Start a new session for each independent task.

---

## Tool use

### Let the agent choose tools

Don't enumerate tool calls in the prompt. Just state the goal — the agent decides which tools to use:

> Find all `TODO` comments across Python files in this repo and summarise them.

Not:

> Use bash to run `grep -r TODO --include="*.py"`, then read each file with read_file and list the todos.

### Guide tool behaviour with rules, not instructions

If you always want the agent to read before writing, put that in the system prompt:

```
- Before editing any file, read it with read_file first.
```

This is more reliable than repeating it in every user turn.

### Requesting specific shell commands

For tasks that map naturally to shell one-liners, say so:

> Run `pytest tests/ -q` and show me the output. If tests fail, fix the failures — but do not change test expectations, only fix the implementation.

### Limiting scope

Safety flags are the right way to limit what the agent can do — not prompts. Prompts can be overridden; flags cannot.

```python
from agent import SafetyConfig

# Lock the agent to reading only — safe for exploration
SafetyConfig(read_only=True)

# Require human approval before any write or shell command
SafetyConfig(require_approval_for_writes=True, require_approval_for_execute=True)
```

---

## Thinking / reasoning models

Some providers surface a chain-of-thought before the final answer. Aar stores these as `ReasoningBlock` events.

| Provider | Reasoning | How to enable |
|---|---|---|
| Anthropic (claude-3-7+) | Extended thinking | `extra={"thinking": {"type": "enabled", "budget_tokens": 5000}}` |
| Ollama deepseek-r1 / qwen3 | `<think>` tags | `extra={"supports_reasoning": True}` |
| OpenAI o1 / o3 | Built-in | Automatic — no config needed |

### Tips for reasoning models

- **Give harder problems.** Reasoning models shine on tasks that require planning, multi-file refactors, or debugging logic errors. For trivial edits they are slower and costlier without benefit.
- **Increase `max_tokens`.** Reasoning consumes tokens before the visible response. Set `max_tokens` to at least 8 000 for complex tasks.
- **Don't prompt-engineer the thinking.** Instructions like "think step by step" or "reason carefully" have no effect — the model is already thinking. Focus the prompt on the task itself.

```python
from agent import AgentConfig, ProviderConfig

config = AgentConfig(
    provider=ProviderConfig(
        name="anthropic",
        model="claude-sonnet-4-5",
        max_tokens=16_000,
        extra={"thinking": {"type": "enabled", "budget_tokens": 8_000}},
    )
)
```

---

## Multimodal input (image · audio · video)

Multimodal-capable models accept images and audio alongside text. Pass a list of `ContentBlock` objects instead of a plain string. Media blocks should come **before** text for best results.

### Quick CLI / TUI attachment

Use the `@file` syntax — aar detects the file type automatically:

```
# image
What error is shown here? @screenshot.png

# audio
Transcribe this recording. @meeting.wav

# mixed
Compare what you see and hear. @chart.png @explanation.mp3
```

Supported types: images (`.png`, `.jpg`, `.gif`, `.webp`, `.bmp`, `.tiff`), audio (`.wav`, `.mp3`, `.ogg`, `.flac`, `.m4a`). Video is typed but not yet wired to any provider.

### Images

```python
from agent.core.events import TextBlock, ImageURLBlock, ImageURL

# HTTP/HTTPS URL
parts = [
    ImageURLBlock(image_url=ImageURL(url="https://example.com/screenshot.png")),
    TextBlock(text="What error is shown in this screenshot?"),
]
response = await agent.chat(parts)

# Local file — base-64 encoded
import base64
data = base64.b64encode(open("diagram.png", "rb").read()).decode()
parts = [
    ImageURLBlock(image_url=ImageURL(url=f"data:image/png;base64,{data}")),
    TextBlock(text="Describe the architecture in this diagram."),
]
response = await agent.chat(parts)

# Or let the helper encode for you
from agent.core.multimodal import file_to_content_block
from pathlib import Path

parts = [
    file_to_content_block(Path("diagram.png")),   # → ImageURLBlock
    TextBlock(text="Describe the architecture."),
]
```

### Audio

> **Limitation:** Ollama's API does not yet support audio input (as of v0.20). The framework types (`AudioBlock`, `AudioData`) exist and audio files can be attached with `@file`, but audio blocks will be **dropped with a warning** when using Ollama. This will work automatically once Ollama adds audio API support. Gemma 4 supports audio at the model level — only the API bridge is missing.

The `AudioBlock` type is ready for providers that support audio (or future Ollama versions):

```python
from agent.core.events import AudioBlock, AudioData, TextBlock
import base64

data = base64.b64encode(open("meeting.wav", "rb").read()).decode()
parts = [
    AudioBlock(audio=AudioData(url=f"data:audio/wav;base64,{data}", format="wav")),
    TextBlock(text="Summarise the key action items from this meeting recording."),
]
response = await agent.chat(parts)

# Or use the helper (detects format from extension)
from agent.core.multimodal import file_to_content_block
parts = [
    file_to_content_block(Path("meeting.wav")),   # → AudioBlock
    TextBlock(text="Summarise the key action items."),
]
```

### Mixed image + audio (future — requires Ollama audio API support)

> **Note:** The image part works now. Audio requires Ollama to add API support — audio blocks are
> currently dropped with a warning. The framework types are ready.

```python
parts = [
    file_to_content_block(Path("dashboard.png")),  # image first — works now
    file_to_content_block(Path("notes.wav")),       # audio — dropped until Ollama supports it
    TextBlock(text="The image is a dashboard screenshot. The audio is my verbal annotation. Describe what I'm pointing out."),
]
```

### Video (prepared, not yet implemented)

`VideoBlock` and `VideoData` types exist for future use — passing a video file currently raises `ValueError`. Use the `@file` CLI syntax with a `.mp4` to see the error message; support will be added once providers expose a stable video API.

### Ollama multimodal models

| Model | Vision | Audio | Notes |
|---|---|---|---|
| `gemma4:e4b` | ✓ | model ✓ / API ✗ | 8B MoE; audio supported by model but not yet by Ollama API |
| `qwen2.5vl:7b` | ✓ | — | Strong vision-only model |
| `llava:13b` | ✓ | — | Reliable vision; good for diagrams |
| `minicpm-v` | ✓ | — | Fast, small vision model |

```bash
ollama pull gemma4:e4b    # vision (audio when Ollama API supports it)
ollama pull qwen2.5vl:7b  # vision only
```

### Image prompting tips

- **Put media before text** — Gemma 4 and most vision models attend better when the image or audio comes first in the content list.
- **Be specific about what to extract.** "What does this image show?" is weak. "List every label visible in this UI screenshot" or "Identify the bottleneck in this flame graph" are far better.
- **One image per question** works better than sending multiple images and asking a single vague question.
- **Use `detail: "high"`** (OpenAI) for dense images like code screenshots, charts, or technical diagrams:
  ```python
  ImageURLBlock(image_url=ImageURL(url="...", detail="high"))
  ```
- **Describe the context** the model doesn't have. "This is a screenshot of a React component failing its snapshot test" is better than just attaching the image.
- **Ask for structured output** when you need to act on the result:
  > Look at this database schema diagram. List every table name and its columns as a JSON object.

### Audio prompting tips (for future use)

> These tips apply once Ollama (or another provider) supports audio input. The framework is ready.

- **Keep clips short and focused** — under 30 seconds per clip for Gemma 4; longer audio should be split.
- **State the task before the clip makes sense** if the audio is ambiguous: "This is a customer support call. Identify the main complaint and the resolution offered."
- **Specify the expected output format**: "Return a JSON object with keys: speaker, summary, action_items."
- **Mono WAV at 16 kHz** is the most portable format across audio-capable models; avoid stereo or high-sample-rate files when compatibility matters.

---

## Provider-specific notes

### Anthropic

- `temperature=0` is deterministic and good for code edits. Raise it (`0.7`–`1.0`) for brainstorming or creative tasks.
- Extended thinking is not compatible with `temperature` — do not set both.
- Tool calls are native; the model will call multiple tools in one step when it sees fit.

### OpenAI

- `gpt-4o` is a strong all-rounder for code + vision.
- `o1` / `o3` are slow but excel at multi-file planning, algorithm design, and tricky debugging. Reserve them for hard problems.
- Set `max_tokens` explicitly; the default may be too low for long code generation tasks.

### Ollama — qwen3.5:9b

`qwen3.5:9b` is the recommended default for local Ollama use. It is a 6.6 GB native vision-language model with a 256 K context window, built-in thinking mode, strong tool calling, and support for 201 languages.

```bash
ollama pull qwen3.5:9b
```

#### Minimal aar config

```python
from agent import AgentConfig, ProviderConfig

config = AgentConfig(
    provider=ProviderConfig(
        name="ollama",
        model="qwen3.5:9b",
        max_tokens=32_768,      # room for thinking + answer; raise to 81 920 for hard problems
        temperature=1.0,
        extra={
            "supports_reasoning": True,   # aar strips <think>…</think> into ReasoningBlock events
            "supports_vision": True,      # Text + Image input
            "supports_tools": True,       # native tool calling
            "num_ctx": 32_768,            # must be set explicitly — Ollama defaults to 2 048
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
        },
    )
)
```

#### Thinking mode

Qwen3.5 always thinks before answering — it emits `<think>…</think>` blocks that aar
captures as `ReasoningBlock` events and strips from the visible response. **There is no
`/nothink` soft-switch** (unlike Qwen3); thinking cannot be disabled via a prompt prefix
when running through Ollama.

Because thinking consumes tokens before the visible answer:

- Set `max_tokens` to **at least 32 768** for everyday tasks.
- For hard math, competitive programming, or multi-file refactors, raise it to **81 920**.
- Never set `max_tokens` lower than ~4 000 or the model may be cut off mid-thought.

```python
# Access the thinking trace from a session
from agent.core.events import ReasoningBlock

for event in session.events:
    if isinstance(event, ReasoningBlock):
        print(event.content)   # the raw chain-of-thought
```

#### Recommended sampling parameters by task

| Task type | `temperature` | `top_p` | `top_k` | `repeat_penalty` |
|-----------|--------------|---------|---------|-----------------|
| General (thinking) | 1.0 | 0.95 | 20 | 1.0 |
| Precise coding / web dev (thinking) | 0.6 | 0.95 | 20 | 1.0 |
| General (non-thinking tasks) | 0.7 | 0.8 | 20 | 1.0 |
| Hard reasoning / math | 1.0 | 1.0 | 40 | 1.05 |

In aar, `temperature` is set directly on `ProviderConfig`; everything else goes in `extra`:

```python
# Precise coding preset
ProviderConfig(
    name="ollama", model="qwen3.5:9b",
    max_tokens=81_920,
    temperature=0.6,
    extra={
        "supports_reasoning": True, "supports_vision": True, "supports_tools": True,
        "num_ctx": 32_768, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
    },
)
```

#### Context window

The Ollama quantised model ships with a **256 K token** context window, but Ollama's
server default is only **2 048 tokens** unless you override it. Always set `num_ctx`
explicitly:

```python
extra={"num_ctx": 32_768}   # safe daily driver — fits most sessions + thinking trace
extra={"num_ctx": 65_536}   # large codebases or long chat sessions
extra={"num_ctx": 131_072}  # maximum practical on ≥ 24 GB VRAM
```

> **Memory note:** every doubling of `num_ctx` roughly doubles KV-cache VRAM. Start at
> 32 768 and increase only if you actually need the context.

#### Vision (image input)

Qwen3.5:9b has a vision encoder — pass images directly alongside text:

```python
from agent.core.events import TextBlock, ImageURLBlock, ImageURL

# HTTP URL
response = await agent.chat([
    TextBlock(text="What does this architecture diagram show?"),
    ImageURLBlock(image_url=ImageURL(url="https://example.com/diagram.png")),
])

# Local file (base-64)
import base64
data = base64.b64encode(open("screenshot.png", "rb").read()).decode()
response = await agent.chat([
    TextBlock(text="List every UI element visible in this screenshot."),
    ImageURLBlock(image_url=ImageURL(url=f"data:image/png;base64,{data}")),
])
```

#### Tool calling

Qwen3.5:9b has excellent tool-calling performance (BFCL-V4: 66.1 for the 9B variant)
and handles multi-step tool chains well. No special config is needed beyond
`"supports_tools": True` — just register your tools normally:

```python
aar chat --provider ollama --model qwen3.5:9b
aar tui  --provider ollama --model qwen3.5:9b --verbose
```

#### What qwen3.5:9b excels at

- **Agentic tasks** — one of the strongest 9B models on tool-use benchmarks (TAU2-Bench: 79.1)
- **Long context** — genuinely useful up to 64 K+ tokens; great for large codebase analysis
- **Vision + reasoning** — MMMU: 78.4, MathVision: 78.9; can reason over charts, diagrams, screenshots
- **Multilingual** — 201 languages; you can mix English instructions with non-English source material
- **Instruction following** — IFEval: 91.5; follows precise, multi-constraint prompts reliably

#### What to watch out for

- **Thinking latency** — the model always thinks first; first-token latency is higher than
  non-thinking models. On CPU or low-VRAM hardware, set a lower `num_ctx` to compensate.
- **Repetition at high temperature** — if you see looping output, add
  `"repeat_penalty": 1.05` to `extra`.
- **`num_ctx` must be explicit** — forgetting it is the most common source of truncated
  responses and confused tool calls.

### Ollama — gemma4:e4b

`gemma4:e4b` (Gemma 4 E4B, 8B MoE) is Google's recommended small multimodal model and the best local option when you need **both image and audio input**. It has a 128 K context window and supports 140+ languages.

```bash
ollama pull gemma4:e4b
```

#### Minimal aar config

```python
ProviderConfig(
    name="ollama",
    model="gemma4:e4b",
    max_tokens=8192,
    temperature=1.0,
    extra={
        "supports_vision": True,   # image input (default True)
        "supports_tools": True,
        "top_p": 0.95,
        "top_k": 64,
        "min_p": 0.0,
    },
)
```

#### Image input

```python
from agent.core.multimodal import file_to_content_block
from agent.core.events import TextBlock
from pathlib import Path

# Put the image BEFORE the text — Gemma 4 attends better in this order
parts = [
    file_to_content_block(Path("chart.png")),
    TextBlock(text="What trend does this chart show? Quote the exact axis labels."),
]
response = await agent.chat(parts)
```

Or from the CLI:
```bash
aar run "What trend does this chart show? @chart.png"
```

#### Audio input (not yet available)

Gemma 4 E4B supports audio at the model level (up to ~30 s), but Ollama's API does not expose audio input as of v0.20. Audio blocks attached with `@file` will be dropped with a warning. The framework types are ready — this will work automatically once Ollama adds audio support.

#### What gemma4:e4b excels at

- **Image understanding** — charts, diagrams, screenshots, OCR, UI analysis
- **Lightweight** — runs comfortably on 8 GB VRAM at Q4 quantisation
- **Multilingual** — 140+ languages

#### What to watch out for

- **Audio not yet available** — Ollama's API doesn't support audio input yet (v0.20). Audio blocks are dropped with a warning.
- **No tool calling with images** — Gemma 4 does not reliably combine tool use and image input in the same turn. Send images in a separate turn before or after tool-heavy turns.
- **HTTP image URLs** are not supported via Ollama's native `/api/chat` endpoint — always base-64 encode local files (the `@file` syntax does this automatically).

---

## Common patterns

### Code review

```python
import base64, pathlib

diff = pathlib.Path("changes.diff").read_text()
await agent.chat(
    f"Review this diff for bugs, style violations, and missing tests. "
    f"Be concise — flag only real problems, not preferences.\n\n```diff\n{diff}\n```"
)
```

### Debugging with a traceback

```python
tb = pathlib.Path("error.log").read_text()
await agent.chat(
    f"Here is a Python traceback from production:\n\n```\n{tb}\n```\n\n"
    "Read the relevant source files and identify the root cause. "
    "Then apply the minimal fix."
)
```

### One-shot code generation

```python
spec = """
Write a Python function `parse_duration(s: str) -> int` that converts
strings like "1h30m", "45s", "2h", "10m5s" to total seconds.
Raise ValueError for unrecognised formats.
Write a pytest parametrised test alongside it.
Put both in a new file: src/duration.py
"""
session = await agent.run(spec)
```

### Iterative refinement

```python
session = None

session = await agent.run("Write a first draft of the migration script in scripts/migrate.py.", session)
session = await agent.run("Add progress logging every 1 000 rows.", session)
session = await agent.run("Wrap the whole script in a try/except that rolls back on any error.", session)
session = await agent.run("Run it with bash: python scripts/migrate.py --dry-run", session)
```

### Image → code

```python
from agent.core.multimodal import file_to_content_block
from agent.core.events import TextBlock
from pathlib import Path

# Screenshot of a UI → implement it
parts = [
    file_to_content_block(Path("mockup.png")),
    TextBlock(text="Implement this UI mockup as a React component using Tailwind CSS. Write it to src/components/Dashboard.tsx."),
]
session = await agent.run(parts)

# Or from the CLI:
# aar run "Implement this mockup as a React component. Write to src/components/Dashboard.tsx. @mockup.png"
```

### Audio → structured notes (future — requires Ollama audio API support)

> **Note:** This pattern is ready at the framework level but requires Ollama to add audio API
> support. Currently audio blocks are dropped with a warning.

```python
from agent.core.multimodal import file_to_content_block
from agent.core.events import TextBlock
from pathlib import Path

parts = [
    file_to_content_block(Path("standup.wav")),
    TextBlock(text="""
This is a 5-minute standup recording. Extract:
- What each person said they completed yesterday
- What they are working on today
- Any blockers mentioned

Return as JSON with one entry per speaker.
"""),
]
response = await agent.chat(parts)

# Or from the CLI:
# aar run "Extract standup notes as JSON. @standup.wav"
```

### Chart / diagram analysis with follow-up

```python
from agent.core.multimodal import file_to_content_block
from agent.core.events import TextBlock
from pathlib import Path

session = None

# Turn 1: describe the chart
session = await agent.run(
    [file_to_content_block(Path("latency_p99.png")),
     TextBlock(text="Describe this latency chart: what are the axes, what time range does it cover, and where are the spikes?")],
    session,
)

# Turn 2: act on what was described (text only — image stays in context)
session = await agent.run(
    "Search the codebase for the code paths most likely responsible for the spikes you identified.",
    session,
)
```

---

## What to avoid

| Anti-pattern | Why it fails | Better approach |
|---|---|---|
| "Do everything in one prompt" | Too many concerns → the model loses track or truncates | Break into focused turns |
| Vague scope ("improve the code") | The agent doesn't know where to stop | Name files, describe outcomes |
| Repeating safety rules in prompts | Rules in prompts can be reasoned around | Use `SafetyConfig` flags |
| Very long context with no compaction | Token cost grows quadratically | Compact sessions periodically |
| Asking the model to remember things across separate sessions | Sessions are independent | Use `--session` to resume |
| Telling a reasoning model to "think step by step" | It is already thinking; adds noise | Trust the model; focus on the task |
| Combining tool calls and images in the same turn (Gemma 4) | Gemma 4 does not reliably do both at once | Send images first, then ask for tool-heavy follow-up in the next turn |
| Expecting audio to work with Ollama | Ollama's API does not support audio input (as of v0.20) | Audio blocks are dropped with a warning; wait for Ollama to add support |
| Using HTTP image URLs with Ollama | Ollama's native `/api/chat` only accepts base-64 | Use local files with `@file` syntax or `file_to_content_block()` |