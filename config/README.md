# Aar Configuration Guide

Preconfigured config files for different LLM providers and use cases. Copy these to your Aar config directory to get started quickly.

## Setup

Copy any config file to your Aar configuration directory:

**Linux / macOS:**
```bash
cp config/samples/config_claude.json ~/.aar/
```

**Windows:**
```bash
Copy-Item config\samples\config_claude.json -Destination $env:USERPROFILE\.aar\
```

Or manually create `~/.aar/` (or `%USERPROFILE%\.aar\` on Windows) and place your chosen config file there.

## Available Configs

| Config | Provider | Model | Best For |
|--------|----------|-------|----------|
| `config_claude.json` | Anthropic | Claude Sonnet 4.6 | General purpose, production-grade reasoning |
| `config_openai.json` | OpenAI | GPT-4o | Fast iteration, multi-modal support |
| `config_deepseek.json` | OpenAI-compatible | Deepseek-R1 | Cost-effective reasoning |
| `config_qwen.json` | Ollama | Qwen 32B | Local inference, offline work |
| `config_gemma.json` | Ollama | Gemma 27B | Lightweight local option |
| `config_omnicoder.json` | Ollama | OmniCoder | Code-focused tasks |
| `config_gemma_cost_sim.json` | Ollama | Gemma + Cost Tracking | Learning cost tracking without API spend |

## Project Rules

Place custom system prompts in `.agent/rules.md` to extend the agent's behavior. See `rules/rules.md` for the minimal ReAct system prompt used by default.

## Troubleshooting

**"Config not found"**
- Ensure the file is in `~/.aar/` (Linux/Mac) or `%USERPROFILE%\.aar\` (Windows)
- Check file name spelling and `.json` extension

**"API key not found"**
- Set the appropriate environment variable (see above)
- Verify it's valid by testing directly: `curl -H "Authorization: Bearer $KEY" https://api.anthropic.com`

**"Permission denied" on tool use**
- Check `denied_paths` and `allowed_paths` in your config
- Ensure `require_approval_for_writes` is false if running non-interactively

**"Out of memory" or "timeout"**
- Reduce `max_tokens`, `max_steps`, or increase `timeout`
- Lower `max_output_chars` to prevent large output bloat

## Learn More

- **[Configuration Reference](../docs/configuration.md)** — Full `AgentConfig` documentation
- **[Safety & Permissions](../docs/safety.md)** — Deny lists, path restrictions, sandbox modes
- **[Providers](../docs/providers.md)** — Provider-specific setup and advanced options
- **[Development](../docs/development.md)** — Programmatic config usage