# Project Rules — Aar Agent Framework

## Code style
- Python 3.11+. Use type hints on all public functions.
- Format with ruff. Follow existing patterns in the codebase.
- Prefer `pathlib.Path` over `os.path`.

## Architecture
- Keep the core loop thin — avoid adding logic to `agent/core/loop.py` unless necessary.
- All events must be typed dataclasses or Pydantic models — no raw dicts in the event stream.
- Providers are pluggable. Never import a specific provider outside `agent/providers/`.
- Tools run in a sandbox. Tool implementations must not bypass `agent/safety/`.

## Testing
- Use pytest + pytest-asyncio. Tests live in `tests/`.
- New features need at least one happy-path test.

## Git
- Commit messages: imperative mood, lowercase, no period.
- Keep PRs focused — one concern per PR.
