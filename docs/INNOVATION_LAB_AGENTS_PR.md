# Opening a PR to `fetchai/innovation-lab-agents` (`staging`)

This document describes how the **nextjs-sandbox-agent** layout was aligned with the Innovation Lab monorepo. The authoritative guide remains **`docs/AGENT_CREATION.md`** and **`docs/AGENT_WORKFLOW.md`** on the `staging` branch of [innovation-lab-agents](https://github.com/fetchai/innovation-lab-agents).

## What was prepared

A full agent tree under **`agents/nextjs-sandbox-agent/`** was built against a checkout of `staging`, including:

| Requirement (from `AGENT_CREATION.md`) | Status |
|------------------------------------------|--------|
| `agent.py` with health + Sentry + `init_db` + `mark_ready` | Done |
| `protocols/chat_proto.py` with DB idempotency + `create_text_chat` | Done |
| `pyproject.toml` with `agents-shared` path dependency | Done |
| `Dockerfile` with **repo root** build context + `agents_shared` copy | Done (includes Node 20 + git) |
| `.env.example` | Done (`DATABASE_URL` required for chat) |
| `README.md` | Updated for monorepo Docker / local run |

Repo-wide **`.cursor/`** rules (payment lifecycle, DB-first chat, PR checks, etc.) already live at the monorepo root; you do **not** duplicate them inside the agent folder unless the team asks for agent-specific rules.

## Prepared clone (this machine)

If you used the same workspace layout as during setup, a shallow clone of **`staging`** with the agent commit already applied may exist at:

`nextjs-sandbox-agent-clean/innovation-lab-agents-temp`

Latest commit on that clone: **`9d8b318`** (`feat(agents): add nextjs-sandbox-agent for staging PR`).

To publish that work to **your** `innovation-lab-agents` fork:

```bash
cd innovation-lab-agents-temp
git remote rename origin upstream
git remote add origin https://github.com/<your-org>/innovation-lab-agents.git
git push -u origin staging:feature/add-nextjs-sandbox-agent
```

Then open the PR on GitHub (**base: `staging`**). Adjust branch names if your fork already uses `staging`.

### Alternative: apply the patch in your fork

In your fork (on `staging` after `git pull upstream staging`):

```bash
git am docs/patches/0001-feat-agents-add-nextjs-sandbox-agent-for-staging-PR.patch
```

Use the patch file from the **`ShyamRV/Nextjs-agent`** branch **`docs/innovation-lab-pr`** (or copy it from this repo under `docs/patches/`).

## How to land the PR

1. **Fork** [fetchai/innovation-lab-agents](https://github.com/fetchai/innovation-lab-agents) (if you have not already).

2. **Clone your fork** and add upstream:

   ```bash
   git clone https://github.com/<your-org>/innovation-lab-agents.git
   cd innovation-lab-agents
   git remote add upstream https://github.com/fetchai/innovation-lab-agents.git
   git fetch upstream
   git checkout staging
   git pull upstream staging
   ```

3. **Create a feature branch** from `staging`:

   ```bash
   git checkout -b feature/add-nextjs-sandbox-agent
   ```

4. **Copy in the agent directory**  
   Copy the entire folder **`agents/nextjs-sandbox-agent/`** from the prepared clone (or from the artifact you were given) into your fork at the same path.

5. **Lockfile** (if you change dependencies):

   ```bash
   cd agents/nextjs-sandbox-agent
   UV_PYTHON=3.11 uv lock
   cd ../..
   ```

6. **Validate locally** (Postgres: use `agents-db/docker-compose.yml.example` as documented in `AGENT_CREATION.md`):

   ```bash
   cd agents/nextjs-sandbox-agent
   cp .env.example .env
   uv sync
   uv run python agent.py
   ```

7. **Docker** (from monorepo root):

   ```bash
   docker build -f agents/nextjs-sandbox-agent/Dockerfile -t nextjs-sandbox-agent:local .
   ```

8. **Push and open PR** targeting **`fetchai/innovation-lab-agents`** → base branch **`staging`**:

   ```bash
   git add agents/nextjs-sandbox-agent
   git commit -m "feat(agents): add nextjs-sandbox-agent"
   git push -u origin feature/add-nextjs-sandbox-agent
   ```

   On GitHub: **Compare & pull request** → base: **`staging`**, compare: your branch.

## CI expectations

On PRs to `staging` / `main`, `.github/workflows/lint.yaml` runs **Ruff check** and **Ruff format** on the whole repo; **`agents/nextjs-sandbox-agent/`** must pass. **`build-and-push.yaml`** builds images when `agents/<name>` changes after merge.

## Infra follow-up

Image name becomes **`nextjs-sandbox-agent`** (from the directory name). After the image exists, **`agents-infra-staging`** (or production infra) may need a Helm values entry so Kubernetes runs the new workload. Coordinate with the platform team using the same process as other agents.

## Differences vs your standalone `ShyamRV/Nextjs-agent` repo

| Standalone repo | Innovation Lab agent |
|-----------------|----------------------|
| `DATABASE_URL` optional for chat | **Required** — chat is fail-closed without `agents_shared` DB |
| No `agents_shared` | **Required** — health, Sentry, DB, `create_text_chat` |
| Single-repo Docker context | **Monorepo root** Docker context |

You can keep developing in **ShyamRV/Nextjs-agent** for speed, then periodically sync the **`agents/nextjs-sandbox-agent/`** subtree into the fork before raising a PR.
