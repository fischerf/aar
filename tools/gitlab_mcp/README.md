# gitlab-issue-mcp

A minimal, extendable **Model Context Protocol (MCP) server** for GitLab Issues,
written in Python. Connect it to Claude Desktop (or any MCP client) and let an
AI read, answer, label, and close your assigned GitLab issues.

---

## File layout

```
gitlab-issue-mcp/
├── server.py          # MCP server — all tools & resources live here
├── gitlab_client.py   # GitLab REST API v4 wrapper — extend the API surface here
├── requirements.txt
└── README.md
```

The two files are intentionally separate:
- **`gitlab_client.py`** — pure API logic, no MCP concepts, easy to unit-test.
- **`server.py`** — pure MCP wiring, imports from `gitlab_client`, no HTTP code.

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Create a GitLab Personal Access Token

GitLab → User Settings → Access Tokens → create token with **`api`** scope.

### 3. Set environment variables

**Linux / macOS (bash/zsh):**
```bash
export GITLAB_URL="https://gitlab.com"        # or your self-hosted instance
export GITLAB_TOKEN="glpat-xxxxxxxxxxxx"
# export GITLAB_USER="youruser"               # optional, informational only
```

**Windows (cmd.exe):**
```bat
set GITLAB_URL=https://gitlab.com
set GITLAB_TOKEN=glpat-xxxxxxxxxxxx
```

> **Windows gotcha:** Do **not** wrap the value in quotes with `set`.
> `set GITLAB_TOKEN="glpat-..."` stores the literal quote characters as part
> of the value, which causes `"GITLAB_TOKEN environment variable is not set"`
> errors even though the variable appears to be set.
> Use `set VAR=value` (no quotes) or `$env:VAR = "value"` in PowerShell.

**Windows (PowerShell):**
```powershell
$env:GITLAB_URL   = "https://gitlab.com"
$env:GITLAB_TOKEN = "glpat-xxxxxxxxxxxx"
```

#### SSL / TLS (internal / self-hosted GitLab)

If your GitLab instance uses a certificate signed by a corporate or
self-signed CA that is not in the system trust store, set one of:

```bash
# Option A — disable verification entirely (trusted internal host only):
export GITLAB_SSL_VERIFY=0

# Option B — point to your corporate CA bundle:
export GITLAB_SSL_VERIFY=/path/to/corp-ca-bundle.pem
```

`GITLAB_SSL_VERIFY` accepts:
| Value | Effect |
|---|---|
| `1` / `true` / unset | Default — verify with system CA store |
| `0` / `false` | Disable certificate verification |
| `/path/to/ca.pem` | Use a custom CA bundle file |

---

## Running

### stdio (Claude Desktop / local)

```bash
python server.py
```

With SSL verification disabled (internal GitLab):

```bash
GITLAB_SSL_VERIFY=0 python server.py
```

### SSE (remote / browser clients)

```bash
python server.py --transport sse
```

### rof_ai_demo (stdio MCP via ROF)

```bash
python rof_ai_demo.py \
    --provider openai \
    --mcp-stdio gitlab-issues python D:/Github/rof/tools/gitlab_mcp/server.py \
    --mcp-ssl-no-verify
```

`--mcp-ssl-no-verify` injects `GITLAB_SSL_VERIFY=0` (and
`PYTHONHTTPSVERIFY=0`) into the subprocess environment automatically —
no need to set the env var manually when launching via the demo.

### rof_ai_demo with knowledge base (MCP + RAG combined)

Pre-load the `knowledge/` directory so the LLM can resolve project names,
understand label conventions, and use domain vocabulary without querying
GitLab for background information:

```bash
python rof_ai_demo.py \
    --provider openai \
    --mcp-stdio gitlab-issues python D:/Github/rof/tools/gitlab_mcp/server.py \
    --mcp-ssl-no-verify \
    --rag-backend chromadb \
    --rag-persist-dir ./knowledge_store \
    --knowledge-dir D:/Github/rof/tools/gitlab_mcp/knowledge
```

On subsequent runs the ChromaDB store is already seeded — omit
`--knowledge-dir` to skip re-indexing:

```bash
python rof_ai_demo.py \
    --provider openai \
    --mcp-stdio gitlab-issues python D:/Github/rof/tools/gitlab_mcp/server.py \
    --mcp-ssl-no-verify \
    --rag-backend chromadb \
    --rag-persist-dir ./knowledge_store
```

---

## Connecting to Claude Desktop

Add this block to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "gitlab-issues": {
      "command": "python",
      "args": ["/absolute/path/to/server.py"],
      "env": {
        "GITLAB_URL": "https://gitlab.com",
        "GITLAB_TOKEN": "glpat-xxxxxxxxxxxx",
        "GITLAB_SSL_VERIFY": "0"
      }
    }
  }
}
```

> **Note:** Set `"GITLAB_SSL_VERIFY": "0"` only when connecting to a
> trusted internal GitLab instance whose certificate is signed by a
> corporate CA not in the system trust store.  Remove it (or set to `"1"`)
> for public GitLab.com.

---

## Available tools

| Tool | What it does |
|---|---|
| `whoami` | Show the authenticated user |
| `list_my_issues` | List your assigned issues (filter by state, labels, project) |
| `read_issue` | Read a full issue + comment thread |
| `answer_issue` | Post a comment on an issue |
| `close_issue` | Close an issue, optionally with a closing comment |
| `reopen_issue` | Reopen a closed issue |
| `label_issue` | Set labels on an issue |
| `find_projects` | Discover project IDs / paths you have access to |

---

## Example prompts

### MCP only

> "List all my open GitLab issues tagged `gDoing`."

> "Read issue 10 of project 303 and save to markdown file."

> "Close issue 14 in project 303 with a comment saying it was resolved."

> "Label issue 5 in project 303 as gDoing."

> "Find all projects in the signatureservices namespace."

### MCP + knowledge base combined

These prompts work best when the `knowledge/` directory is loaded via
`--knowledge-dir` (or already in ChromaDB):

> "What is the project ID for the KGS content service?"

RAGTool looks up `projects.md` → answers `303` without a live API call.

> "Read my open issues in the storage backend project and summarise them."

RAGTool resolves "storage backend" → project 311, then MCPClientTool
fetches the live issues.

> "List my gDoing issues and explain what gDoing means."

RAGTool retrieves the label definition from `labels_and_workflow.md`
alongside the live MCP results.

> "Read issue 10 of project 303 and check if it relates to ILM or metadata persistence."

MCPClientTool fetches the live issue; RAGTool retrieves the domain
context from `labels_and_workflow.md`; the LLM synthesises both.

> "Which of my open issues are blocked?"

MCPClientTool lists issues; RAGTool explains the gBlocked convention.

---

## Knowledge base

The `knowledge/` subdirectory contains pre-built context documents that the
ROF `RAGTool` indexes at startup.  They are static files — update them when
your project structure changes.

| File | Contents |
|------|----------|
| `knowledge/projects.md` | Numeric ID → path mapping for every project, domain groupings, and plain-language aliases (e.g. "the storage backend" → 311) |
| `knowledge/labels_and_workflow.md` | Label conventions (`gTodo`, `gDoing`, `gDone`, …), workflow transitions, project-specific notes, and the BSI TR-ESOR / SecDocs domain glossary |

### When to use RAGTool alongside MCP

| Situation | Without knowledge base | With knowledge base |
|-----------|----------------------|---------------------|
| "read an issue in the KGS content service" | Must know project ID 303 | RAG resolves the name automatically |
| "what does gDoing mean?" | LLM guesses | RAG retrieves the exact definition |
| "list my ILM-related issues" | No context for "ILM" | RAG maps ILM to project 303 |
| "explain this XAIP issue" | LLM uses general knowledge | RAG provides SecDocs-specific TR-ESOR context |

### Keeping the knowledge base up to date

Run `find_projects` via `test_mcp.py` to get the current project list, then
update `knowledge/projects.md` manually:

```bash
python test_mcp.py --no-verify --tool find_projects
```

Re-seed ChromaDB after any update by passing `--knowledge-dir` once:

```bash
python rof_ai_demo.py ... \
    --rag-backend chromadb \
    --rag-persist-dir ./knowledge_store \
    --knowledge-dir D:/Github/rof/tools/gitlab_mcp/knowledge
```

---

## Extending

### Add a new API method

Open `gitlab_client.py` and add a function using the `_get` / `_post` / `_put`
helpers:

```python
def list_merge_requests(project_id: str | int, state: str = "opened") -> list[dict]:
    return _get(f"/projects/{project_id}/merge_requests", {"state": state})
```

### Expose it as an MCP tool

Open `server.py` and add a decorated function:

```python
@mcp.tool()
def list_mrs(
    project_id: Annotated[str, "Project ID or namespace/repo"],
    state: Annotated[str, "opened | closed | merged | all"] = "opened",
) -> str:
    """List merge requests for a project."""
    mrs = gl.list_merge_requests(project_id, state)
    return "\n".join(f"!{m['iid']} {m['title']} ({m['state']})" for m in mrs)
```

That's it — restart the server and the new tool appears in your MCP client.

---

## License

Apache License 2.0 
