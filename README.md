# nextjs-sandbox-agent

Autonomous **uAgents** backend using the **Fetch.ai Agent Chat Protocol**. Accepts natural-language descriptions via chat and generates a real, runnable **Next.js 14 + TypeScript + Tailwind CSS** application in an isolated workspace — then installs, builds, self-heals on errors, optionally previews (local or **E2B** public URL), and optionally publishes to a **GitHub** repository.

## Architecture

```
protocols/chat_proto.py   ← ACK, extract text, stream ask()
agent.py                  ← uAgent entrypoint, startup, include protocol
ai/ai.py                  ← LangGraph create_agent, in-memory or Postgres checkpointer
ai/PROMPT.md              ← System prompt (full instructions for the LLM)
ai/tools.py               ← LangChain @tool: write files, npm, preview, GitHub
ai/models.py              ← UserContext + SandboxResponse Pydantic models
ai/llm_config.py          ← ASI:One base URL / model / session header
ai/session_workspace.py   ← Per-session workspace reuse for follow-up edits
sandbox/scaffold.py       ← Static Next.js 14 scaffold files
sandbox/runner.py         ← npm subprocess helpers + readiness probe
sandbox/preview_registry.py ← Track dev servers and E2B sandboxes for cleanup
sandbox/e2b_preview.py    ← Upload workspace → E2B → public HTTPS URL
sandbox/github_device.py  ← GitHub OAuth device flow (user's own account)
sandbox/github_publish.py ← git init / commit / push to GitHub repo
```

## Requirements

- **Python 3.11+** with [`uv`](https://github.com/astral-sh/uv)
- **Node.js 20+** and **npm** on `PATH`
- **`git`** on `PATH` (only needed for GitHub publish)
- **ASI:One API key** (`ASI_ONE_API_KEY`)

PostgreSQL is **optional** — without `DATABASE_URL` the agent uses an in-memory LangGraph checkpointer (conversation state is lost on restart).

## Quick start

```bash
cp .env.example .env
# Fill in: AGENT_SEED, ASI_ONE_API_KEY
uv sync
uv run python agent.py
```

## Docker

```bash
docker build -t nextjs-sandbox-agent:local .
docker run --rm --env-file .env -p 8029:8029 nextjs-sandbox-agent:local
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_SEED` | — | **Required.** Agent identity seed. |
| `ASI_ONE_API_KEY` | — | **Required.** ASI:One API key. |
| `ASI_ONE_BASE_URL` | `https://api.asi1.ai/v1` | LLM base URL override. |
| `ASI_ONE_MODEL` | `asi1` | LLM model override. |
| `DATABASE_URL` | _(none)_ | Postgres URI for persistent LangGraph checkpointer. |
| `NEXTJS_SESSION_WORKSPACE` | `true` | Reuse one workspace per session for follow-up edits. |
| `SKIP_PREVIEW_DEV` | `false` | Disable preview after build. |
| `PREVIEW_USE_NEXT_DEV` | `false` | Use `next dev` instead of `next start` (production). |
| `PREVIEW_PUBLIC_BASE_URL` | _(none)_ | Base URL for local preview (e.g. `https://myhost.example.com`). |
| `PREVIEW_KEEPALIVE_SEC` | `600` | Seconds before preview and workspace cleanup. |
| `E2B_API_KEY` | _(none)_ | Enables E2B cloud preview (public HTTPS URL). |
| `PREVIEW_USE_E2B` | `true` (if key set) | Set `false` to force local preview. |
| `GITHUB_OAUTH_CLIENT_ID` | _(none)_ | GitHub OAuth App Client ID (device flow) for per-user repos. |
| `GITHUB_TOKEN` | _(none)_ | Operator bot PAT fallback for GitHub publish. |
| `SANDBOX_BUILD_MAX_RETRIES` | `5` | Max `npm run build` attempts per workspace. |

## How it works

1. **User** sends a message via ASI1 Chat Interface (Agentverse).
2. **chat_proto** extracts text, ACKs, calls `ai.ask()`.
3. **ai.py** creates a LangGraph agent with the `PROMPT.md` system prompt and the tool list.
4. The LLM (ASI:One) calls tools to **scaffold → write → install → build → preview → publish**.
5. On build failure: reads logs, patches files, retries (up to `SANDBOX_BUILD_MAX_RETRIES`).
6. The final `SandboxResponse` (Status / Summary / Preview URL / GitHub URL / Logs) is sent back to the user.

## Troubleshooting

**`next: command not found` / exit 127**
The sandbox prepends `node_modules/.bin` to `PATH` and uses `npx --no-install next` in scripts. Confirm `npm install` completed successfully and `node`/`npm` are a normal install (not a broken shim).

**Preview `Internal Server Error`**
Default preview is `next start` (production) after a successful build. If you still see 500, enable `SANDBOX_KEEP_WORKSPACES=true`, reproduce, then run `npm run start` manually and read the server log.

**`ERR_CONNECTION_REFUSED` on `127.0.0.1`**
The URL is only accessible on the machine running the agent. Use `PREVIEW_PUBLIC_BASE_URL`, an SSH tunnel, or set `E2B_API_KEY` for a public URL instead.
