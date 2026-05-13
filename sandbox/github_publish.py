"""Create a GitHub repository and push the workspace (GitHub REST API + local ``git``).

Tokens (first match wins):

1. **Per-user OAuth** — ``.agent_github_user_token`` in the workspace (written by
   ``complete_github_device_login`` after ``begin_github_device_login``). Repos are created
   under **that** GitHub user.
2. **Operator fallback** — ``GITHUB_TOKEN`` in the agent environment (PAT / bot account).

If ``GET /repos/{owner}/{repo}`` succeeds, creation is skipped and the workspace is pushed to the
existing repository (follow-up edits such as root ``README.md``).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

import httpx

from sandbox.github_device import user_token_path

_DEFAULT_GITIGNORE = """# Next.js sandbox agent — do not commit secrets
node_modules/
.next/
out/
.env
.env*.local
.vercel
*.log
.DS_Store
.agent_build_attempts
.agent_github_device.json
.agent_github_user_token
"""


def resolve_github_token(workspace: Path) -> str | None:
    """User-linked OAuth token in workspace, else operator ``GITHUB_TOKEN``."""
    p = user_token_path(workspace)
    if p.is_file():
        t = p.read_text(encoding="utf-8").strip()
        if t:
            return t
    op = (os.getenv("GITHUB_TOKEN") or "").strip()
    return op or None

_REPO_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]{1,100}$")


def _api_base() -> str:
    return (os.getenv("GITHUB_API_URL") or "https://api.github.com").rstrip("/")


def _validate_repo_name(name: str) -> str | None:
    s = name.strip()
    if not s:
        return "repo_name must be non-empty"
    if not _REPO_NAME_RE.match(s):
        return "repo_name: use only letters, digits, ., _, - (max 100 chars)"
    if s in {".", ".."} or ".." in s:
        return "invalid repo_name"
    return None


def _ensure_gitignore(workspace: Path) -> None:
    p = workspace / ".gitignore"
    if not p.is_file():
        p.write_text(_DEFAULT_GITIGNORE, encoding="utf-8")
        return
    text = p.read_text(encoding="utf-8")
    for line in _DEFAULT_GITIGNORE.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s not in text.splitlines():
            text = text.rstrip() + "\n" + s + "\n"
    p.write_text(text, encoding="utf-8")


def _run_git(
    workspace: Path, args: list[str], *, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, **(env or {})},
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _git_identity_local(workspace: Path) -> None:
    _run_git(workspace, ["config", "user.email", "sandbox-agent@local"])
    _run_git(workspace, ["config", "user.name", "Next.js Sandbox Agent"])


def _set_origin(workspace: Path, remote: str) -> None:
    code, stdout, _ = _run_git(workspace, ["remote"])
    if code == 0 and "origin" in stdout:
        _run_git(workspace, ["remote", "set-url", "origin", remote])
    else:
        _run_git(workspace, ["remote", "add", "origin", remote])


def publish_workspace_to_github(
    workspace: Path,
    repo_name: str,
    description: str,
    *,
    private: bool,
    organization: str | None,
) -> str:
    """Create repo on GitHub and push ``workspace`` on ``main`` (or the repo default branch)."""
    err = _validate_repo_name(repo_name)
    if err:
        raise RuntimeError(err)

    token = resolve_github_token(workspace)
    if not token:
        raise RuntimeError(
            "No GitHub token available. Either:\n"
            "• **Your account (recommended):** run **begin_github_device_login**, open GitHub, "
            "enter the code, approve access, then run **complete_github_device_login** "
            "(server needs **GITHUB_OAUTH_CLIENT_ID** from a GitHub OAuth App with device flow enabled);\n"
            "• **Operator bot:** set **GITHUB_TOKEN** on the agent host (classic PAT with **repo** scope, "
            "or fine-grained **Contents** write)."
        )

    _ensure_gitignore(workspace)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    org = (organization or "").strip() or None
    with httpx.Client(timeout=60.0) as client:
        u = client.get(f"{_api_base()}/user", headers=headers)
        if u.status_code != 200:
            raise RuntimeError(f"GitHub user lookup failed: HTTP {u.status_code} {u.text[:500]}")
        login = str((u.json() or {}).get("login") or "").strip()
        if not login:
            raise RuntimeError("GitHub API returned no user login")

        if org:
            create_url = f"{_api_base()}/orgs/{quote(org, safe='')}/repos"
        else:
            create_url = f"{_api_base()}/user/repos"

        body = {
            "name": repo_name.strip(),
            "description": (description or "")[:350],
            "private": bool(private),
            "auto_init": False,
        }

        owner_key = org or login
        repo_slug = repo_name.strip()
        repo_api_url = (
            f"{_api_base()}/repos/{quote(owner_key, safe='')}/{quote(repo_slug, safe='')}"
        )

        chk = client.get(repo_api_url, headers=headers)
        if chk.status_code == 200:
            data = chk.json() or {}
        elif chk.status_code == 404:
            r = client.post(create_url, headers=headers, json=body)
            if r.status_code in (200, 201):
                data = r.json() or {}
            elif r.status_code == 422 and "already exists" in (r.text or "").lower():
                chk2 = client.get(repo_api_url, headers=headers)
                if chk2.status_code != 200:
                    raise RuntimeError(
                        "Create repo failed (name may already exist) and re-fetch failed: "
                        f"HTTP {chk2.status_code} {chk2.text[:500]}"
                    )
                data = chk2.json() or {}
            else:
                raise RuntimeError(
                    f"Create repo failed: HTTP {r.status_code} {r.text[:1200]}"
                )
        else:
            raise RuntimeError(
                f"GitHub repo lookup failed: HTTP {chk.status_code} {chk.text[:500]}"
            )

        html_url = str(data.get("html_url") or "").strip()
        default_branch = str(data.get("default_branch") or "main").strip() or "main"

    owner = org or login
    remote = (
        f"https://x-access-token:{quote(token, safe='')}@github.com/"
        f"{quote(owner, safe='')}/{quote(repo_name.strip(), safe='')}.git"
    )

    if not (workspace / ".git").is_dir():
        code, out, err = _run_git(workspace, ["init", "-b", default_branch])
        if code != 0:
            raise RuntimeError(f"git init failed: {err or out}")
        _git_identity_local(workspace)
    else:
        _git_identity_local(workspace)
        code, out, err = _run_git(workspace, ["rev-parse", "--abbrev-ref", "HEAD"])
        cur = (out or "").strip()
        if cur and cur != default_branch:
            _run_git(workspace, ["branch", "-M", default_branch])

    _run_git(workspace, ["add", "-A"])
    code, out, err = _run_git(workspace, ["status", "--porcelain"])
    if not (out or "").strip():
        raise RuntimeError("Nothing to commit — workspace has no changes to push.")

    code, out, err = _run_git(
        workspace,
        ["commit", "-m", "Update from Next.js sandbox agent"],
    )
    if code != 0:
        raise RuntimeError(f"git commit failed (exit={code}): {(err or out)[-4000:]}")

    _set_origin(workspace, remote)

    code, out, err = _run_git(
        workspace,
        ["push", "-u", "origin", "HEAD:" + default_branch],
    )
    if code != 0:
        raise RuntimeError(f"git push failed (exit={code}): {(err or out)[-4000:]}")

    if not html_url:
        html_url = f"https://github.com/{owner}/{repo_name.strip()}"
    return html_url
