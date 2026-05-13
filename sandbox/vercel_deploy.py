"""Deploy a local Next.js workspace to Vercel using the Vercel REST API.

Uses ``Authorization: Bearer`` with (in order of preference when the caller omits a token):

- **``VERCEL_TOKEN``** — Personal Access Token from https://vercel.com/account/tokens
  (required for most ``POST /v13/deployments`` usage; OAuth user tokens often get HTTP 403).
- **Per-user OAuth** — ``.agent_vercel_user_token.json`` in the workspace (``complete_vercel_oauth``).

``POST https://api.vercel.com/v13/deployments`` — same token family as dashboard tokens;
OAuth access tokens are documented at https://vercel.com/docs/sign-in-with-vercel/tokens
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from sandbox.vercel_oauth import resolve_vercel_access_token

logger = logging.getLogger(__name__)

_DEFAULT_API_BASE = "https://api.vercel.com"
_POLL_INTERVAL_SEC = 3.0
_POLL_TIMEOUT_SEC = 120.0

_SKIP_DIR_NAMES = frozenset({"node_modules", ".next", ".git"})
_SLUG_CHARS_RE = re.compile(r"^[a-z0-9-]+$")


def _api_base() -> str:
    base = (os.getenv("VERCEL_API_BASE") or _DEFAULT_API_BASE).strip().rstrip("/")
    return base or _DEFAULT_API_BASE


def _resolve_team_id(team_id: str | None) -> str | None:
    tid = (team_id or "").strip() or (os.getenv("VERCEL_TEAM_ID") or "").strip()
    return tid or None


def _team_query(team_id: str | None) -> dict[str, str]:
    if not team_id:
        return {}
    return {"teamId": team_id}


def _should_skip_relative(rel_posix: str) -> bool:
    parts = rel_posix.split("/")
    for p in parts:
        if p in _SKIP_DIR_NAMES:
            return True
        if p.startswith(".agent_"):
            return True
    name = parts[-1] if parts else rel_posix
    if name == ".env":
        return True
    if name.endswith(".log"):
        return True
    return False


def _collect_workspace_files(workspace_root: Path) -> list[dict[str, str]]:
    root = workspace_root.resolve()
    out: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if _should_skip_relative(rel):
            continue
        data_b64 = base64.standard_b64encode(path.read_bytes()).decode("ascii")
        out.append({"file": rel, "data": data_b64, "encoding": "base64"})
    if not out:
        raise RuntimeError("No files to upload after filtering (workspace empty or all skipped).")
    return out


def _ensure_https(url: str) -> str:
    u = (url or "").strip()
    if not u:
        raise RuntimeError("Vercel returned an empty deployment URL.")
    if u.startswith("http://") or u.startswith("https://"):
        return u
    return f"https://{u.lstrip('/')}"


def _public_url_from_deployment(data: dict[str, Any], fallback_name: str) -> str:
    u = str(data.get("url") or "").strip()
    if u:
        return _ensure_https(u)
    aliases = data.get("alias") or data.get("aliases")
    if isinstance(aliases, list) and aliases:
        first = str(aliases[0] or "").strip()
        if first:
            return _ensure_https(first)
    return _ensure_https(f"{fallback_name}.vercel.app")


async def deploy_to_vercel(
    workspace_root: Path,
    project_name: str,
    *,
    vercel_token: str,
    team_id: str | None = None,
    on_log: Callable[[str], None] | None = None,
) -> str:
    """
    Deploy the Next.js workspace to Vercel via the REST API.

    Returns the live Vercel URL (e.g. https://my-project.vercel.app).
    """
    def log(msg: str) -> None:
        if on_log:
            on_log(msg)
        else:
            logger.info("%s", msg)

    token = (vercel_token or "").strip()
    if not token:
        token = (os.getenv("VERCEL_TOKEN") or "").strip()
    if not token:
        token = resolve_vercel_access_token(workspace_root) or ""
    if not token:
        raise RuntimeError(
            "No Vercel credentials. Either:\n"
            "• **Per-user:** run **begin_vercel_oauth** (loopback callback auto-completes when `VERCEL_OAUTH_REDIRECT_URI` is `http://127.0.0.1:…`); otherwise paste the callback URL into **complete_vercel_oauth**;\n"
            "• **Operator:** set **VERCEL_TOKEN** on the agent host (PAT from "
            "https://vercel.com/account/tokens )."
        )

    name = project_name.strip().lower()
    if not (1 <= len(name) <= 52):
        raise RuntimeError("vercel_project_name must be 1–52 characters.")
    if not _SLUG_CHARS_RE.match(name) or name.startswith("-") or name.endswith("-"):
        raise RuntimeError(
            "vercel_project_name must be lowercase letters, digits, and hyphens only "
            "(no leading/trailing hyphen)."
        )

    tid = _resolve_team_id(team_id)
    base = _api_base()
    files = _collect_workspace_files(workspace_root)
    log(f"Vercel API: uploading {len(files)} source file(s) to {base}/v13/deployments …")

    payload: dict[str, Any] = {
        "name": name,
        "files": files,
        "projectSettings": {"framework": "nextjs"},
        "target": "production",
    }
    q = _team_query(tid)
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=300.0) as client:
        create = await client.post(
            f"{base}/v13/deployments",
            headers=headers,
            params=q,
            json=payload,
        )
        if create.status_code >= 400:
            body = (create.text or "")[:2000]
            hint = ""
            if create.status_code == 403:
                hint = (
                    "\n\n**Likely cause:** Sign-in-with-Vercel OAuth access tokens (`vca_…`) "
                    "often **cannot** create deployments via `POST /v13/deployments` (Vercel returns 403). "
                    "**Fix:** create a Personal Access Token at https://vercel.com/account/tokens "
                    "and set **`VERCEL_TOKEN`** in `.env` on the agent host, then restart. "
                    "When `VERCEL_TOKEN` is set, deploy uses the PAT instead of workspace OAuth."
                )
            raise RuntimeError(
                f"Vercel create deployment failed: HTTP {create.status_code} {body}{hint}"
            )
        try:
            dep = create.json()
        except Exception as exc:
            raise RuntimeError(f"Vercel create deployment: invalid JSON: {create.text[:800]}") from exc
        if not isinstance(dep, dict):
            raise RuntimeError(f"Vercel create deployment: unexpected body: {dep!r}")

        deployment_id = str(dep.get("id") or "").strip()
        if not deployment_id:
            raise RuntimeError(f"Vercel create deployment: missing id in {json.dumps(dep)[:2000]}")

        preview_url = _public_url_from_deployment(dep, name)
        log(f"Vercel API: deployment {deployment_id!r} created; polling until READY …")

        deadline = time.monotonic() + _POLL_TIMEOUT_SEC
        last_state = ""
        while time.monotonic() < deadline:
            poll = await client.get(
                f"{base}/v13/deployments/{deployment_id}",
                headers=headers,
                params=q,
            )
            if poll.status_code >= 400:
                raise RuntimeError(
                    f"Vercel deployment status failed: HTTP {poll.status_code} "
                    f"{(poll.text or '')[:1200]}"
                )
            try:
                info = poll.json()
            except Exception as exc:
                raise RuntimeError(f"Vercel status: invalid JSON: {poll.text[:800]}") from exc
            if not isinstance(info, dict):
                raise RuntimeError("Vercel status: expected JSON object")

            state = str(info.get("readyState") or info.get("state") or "").upper()
            if state and state != last_state:
                log(f"Vercel API: readyState={state}")
                last_state = state

            if state == "READY":
                return _public_url_from_deployment(info, name)
            if state in {"ERROR", "CANCELED"}:
                raise RuntimeError(
                    f"Vercel deployment ended with {state}. "
                    f"Details: {json.dumps(info)[:2500]}"
                )
            await asyncio.sleep(_POLL_INTERVAL_SEC)

        raise RuntimeError(
            f"Vercel deployment timed out after {_POLL_TIMEOUT_SEC:.0f}s "
            f"(last readyState={last_state!r}). Preview URL was {preview_url}."
        )
