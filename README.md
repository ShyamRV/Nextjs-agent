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

## Deploy and test on Agentverse

This agent uses **`mailbox=True`** (see `agent.py`), so it does **not** need a public inbound IP. Outbound traffic reaches **Agentverse**; the mailbox stores chat until your process polls.

### 1. Host the process

Run it anywhere that can make **HTTPS outbound** (your PC, a VM, Docker, Kubernetes):

- **Docker:** `docker build -t nextjs-sandbox-agent:local .` then `docker run --rm --env-file .env -p 8029:8029 nextjs-sandbox-agent:local`
- **Bare metal:** `uv sync` then `uv run python agent.py`

Required env: **`AGENT_SEED`**, **`ASI_ONE_API_KEY`**. Optional: **`AGENTVERSE_URL`** (default `https://agentverse.ai`), **`AGENT_PORT`** (default `8029`).

### 2. Connect the mailbox once

On first start, logs include an **Agent inspector** URL (Agentverse). Open it, choose **Mailbox**, and complete the connect flow so this running instance is bound to your agent identity. See [uAgents mailbox](https://uagents.fetch.ai/docs/agentverse/mailbox).

### 3. Chat from Agentverse / ASI

Use the **ASI:One** / Agentverse chat UI to open a session with your agent’s **address** (printed at startup as `agent1q…`). Send a short prompt (e.g. “Build a one-page Next.js landing with Tailwind”) and confirm you get ACK + streamed updates + a final reply.

### 4. Production tips

- Keep **one** long-running process per `AGENT_SEED` (avoid duplicate `AGENT_PORT` / duplicate mailbox clients).
- For **E2B** or **GitHub** from cloud hosts, set the same API keys/tokens in the host `.env` as you use locally.
- Set **`PAYMENT_BYPASS=true`** until payment clients are wired, if you hit the free-tier preview limit during testing.

### 5. Let other people use it (public multi-user)

**Agentverse “Hosted Agents”** (the in-browser editor on [agentverse.ai](https://agentverse.ai)) are **not** a drop-in host for this project. They are intentionally **lightweight** tasks (global state resets each call; [allowed imports](https://docs.agentverse.ai/documentation/advanced-usages/allowed-imports) are restricted). This agent needs a normal OS with **Python, Node/npm, git**, long-running processes, and subprocesses — so it matches the **Local / mailbox** model in the [uAgents agent types](https://uagents.fetch.ai/docs/guides/types) guide, not the hosted-editor model.

**What actually works for “everyone can chat with my agent”:**

1. **Run this repo 24/7** on infrastructure you control (VM, Kubernetes, or `docker run` on a cloud host) with outbound HTTPS. Same `AGENT_SEED` every time so the **`agent1q…` address never changes**.
2. **Leave mailbox connected** (or complete the Agent inspector **Mailbox** flow once per deployment if Agentverse asks you to). Users message that address from **ASI:One / Agentverse**; they do not need your IP or port.
3. **Share the address** (and optionally a short description). That is enough for multi-user chat over Agentverse.
4. **Optional discoverability:** Almanac / marketplace listing may require **funds** on the agent wallet (you may see a warning at startup). A **Proxy** setup ([uAgents proxy guide](https://uagents.fetch.ai/docs/agentverse/proxy)) needs a **public URL** to your agent port and is aimed at continuous reachability and visibility; mailbox mode is usually simpler behind NAT.

**Operator responsibility:** anyone who knows the address can consume your **ASI:One**, **E2B**, **GitHub**, etc. credentials within whatever limits you configured. Use **`PAYMENT_BYPASS=false`**, quotas, and secrets scoped to a dedicated account when opening the agent beyond trusted testers.

## Troubleshooting

**`next: command not found` / exit 127**
The sandbox prepends `node_modules/.bin` to `PATH` and uses `npx --no-install next` in scripts. Confirm `npm install` completed successfully and `node`/`npm` are a normal install (not a broken shim).

**Preview `Internal Server Error`**
Default preview is `next start` (production) after a successful build. If you still see 500, enable `SANDBOX_KEEP_WORKSPACES=true`, reproduce, then run `npm run start` manually and read the server log.

**`ERR_CONNECTION_REFUSED` on `127.0.0.1`**
The URL is only accessible on the machine running the agent. Use `PREVIEW_PUBLIC_BASE_URL`, an SSH tunnel, or set `E2B_API_KEY` for a public URL instead.

**`WinError 10048` / `address already in use` on port 8029**
Only one agent can listen on `AGENT_PORT` (default `8029`). Stop the other `uv run python agent.py` (Ctrl+C in its terminal), or set `AGENT_PORT` to a free port in `.env`. On Windows: `Get-NetTCPConnection -LocalPort 8029` to find the owning process.

**Vercel sign-in (`begin_vercel_oauth`)**
Use `VERCEL_OAUTH_REDIRECT_URI=http://127.0.0.1:3939/oauth/vercel/callback` (with that URL registered on the Vercel OAuth app). The agent listens on that port and completes OAuth without pasting a URL. If the port is busy, change the port in `.env` and in the Vercel app. For HTTPS or remote callbacks, set `VERCEL_OAUTH_AUTO_CALLBACK=false` and paste the callback URL into `complete_vercel_oauth`. File deploys from this agent still need `VERCEL_TOKEN` (PAT) in most cases.
