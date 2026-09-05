# Aar Configuration Guide

Reference config files, distro profiles, and rule templates. Copy these into your
Aar config directory to get started quickly.

> Prefer `aar init` — it writes `~/.aar/config.json` derived from the live
> `AgentConfig` defaults (so it always matches the current schema), plus
> `mcp_servers.json`, the pricing template, distro profiles, and theme
> templates. The files here are hand-maintained references for when you want to
> see every option, or need a purpose-built starting point.

## Setup

**Linux / macOS:**
```bash
mkdir -p ~/.aar && cp config/samples/config.json ~/.aar/config.json
```

**Windows:**
```powershell
New-Item -ItemType Directory -Force $env:USERPROFILE\.aar
Copy-Item config\samples\config.json -Destination $env:USERPROFILE\.aar\config.json
```

## What's here

| Path | Purpose |
|------|---------|
| `samples/config.json` | Exhaustive reference config — every `AgentConfig` field, with a multi-provider registry (Anthropic, OpenAI, Gemini, Ollama) |
| `samples/config_terminalbench20.json` | Purpose-built benchmark config: WSL sandbox, approval gates off, verbose logging |
| `distros/alpine-base.json` | WSL sandbox profile — Alpine + Python |
| `distros/alpine-r.json` | WSL sandbox profile — Alpine + Python + R |
| `distros/ubuntu.json` | WSL sandbox profile — Ubuntu 24.04 + Python |
| `rules/rules.md` | Minimal ReAct system prompt used by default |
| `rules/rules.d/wsl_python.md` | Drop-in rule for Python work under the WSL sandbox |

### One config, many providers

`samples/config.json` defines every provider under `providers` and selects the
active one with the top-level `"provider"` key. Switch without editing the file:

```bash
aar chat --provider claude
aar chat --provider gemini-flash
aar chat --provider qwen3.5
```

Set the matching API key env var (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`GEMINI_API_KEY`); Ollama entries need only a running local server.

### Distro profiles

Point `safety.sandbox.wsl.profile` at one of the `distros/*.json` files and it
supplies the rootfs URL, checksum, packages, and prompt hint:

```json
{ "safety": { "sandbox": { "mode": "wsl", "wsl": { "profile": "~/.aar/distros/ubuntu.json" } } } }
```

Each profile carries a `rootfs_sha256`. Keep it — without a checksum the rootfs
download is unverified, and `aar sandbox setup` will warn.

## Project Rules

Place custom system prompts in `.agent/rules.md` to extend the agent's behavior.
See `rules/rules.md` for the minimal ReAct system prompt used by default.

## Troubleshooting

**"Config not found"**
- Ensure the file is at `~/.aar/config.json` (Linux/Mac) or `%USERPROFILE%\.aar\config.json` (Windows)
- Check file name spelling and `.json` extension

**"API key not found"**
- Set the appropriate environment variable (see above), or put it in `providers.<name>.api_key`
- Note that the *agent's own shell* deliberately does not see your API keys — see below

**"Permission denied" on tool use**
- Check `denied_paths` and `allowed_paths` in your config
- For non-interactive runs, set `require_approval_for_writes` / `require_approval_for_execute` to `false`
- If a shell command is refused outright it hit the command deny-list. That list
  now looks through `;`, `&&`, `|`, `sudo` and `sh -c '…'`, so a compound command
  is denied if *any* part of it matches. Override with `safety.denied_commands`
  (token patterns) or `safety.denied_command_patterns` (regexes); `[]` disables
  either one.

**Environment variable is empty inside `bash`**
- By design: the sandbox passes only allow-listed variables, and always strips
  `*_API_KEY`, `*_TOKEN`, `*SECRET*`, `*PASSWORD*`, `AWS_*` and
  `GOOGLE_APPLICATION_CREDENTIALS` so a prompt-injected model can't exfiltrate them.
- Add the names you need to `safety.sandbox.<mode>.allowed_env_vars`, and if a
  needed name matches a deny pattern, narrow `safety.sandbox.env_denylist_patterns`.

**Project extension in `.agent/extensions/` didn't load**
- Untrusted project extensions are skipped. `aar chat` / `aar tui` prompt once;
  `aar run` / `aar serve` / `aar acp` never load them.
- Opt in with `--trust-project-extensions`, `AAR_TRUST_PROJECT_EXTENSIONS=1`, or
  `"trust_project_extensions": true`.

**`aar serve` returns 401**
- Every route but `/health` needs `Authorization: Bearer <token>`. The token is
  printed once at startup, or set it with `--token` / `$AAR_HTTP_TOKEN`.
- Tool calls needing approval are refused by default — pass `--approval auto`
  to allow unattended execution.

**"Out of memory" or "timeout"**
- Reduce `max_tokens`, `max_steps`, or increase `timeout`
- Lower `max_output_chars` to prevent large output bloat
- `tools.command_timeout` is also the hard cap on the `timeout` the model may
  request for a `bash` call

## Learn More

- **[Configuration Reference](../docs/configuration.md)** — Full `AgentConfig` documentation
- **[Safety & Permissions](../docs/safety.md)** — Deny lists, path restrictions, sandbox modes, env filtering
- **[Providers](../docs/providers.md)** — Provider-specific setup and advanced options
- **[Extensions](../docs/extensions.md)** — Discovery tiers and project-extension trust
- **[Web API](../docs/web-api.md)** — `aar serve` auth, CORS, and approval model
- **[Development](../docs/development.md)** — Programmatic config usage
