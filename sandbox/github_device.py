"""GitHub OAuth 2.0 device flow — link the **chat user’s** GitHub account (no PAT in chat)."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx


@contextlib.contextmanager
def _suppress_httpx_info_logs() -> Iterator[None]:
    """Avoid one INFO line per OAuth poll in agent logs."""
    log = logging.getLogger("httpx")
    prev = log.level
    log.setLevel(logging.WARNING)
    try:
        yield
    finally:
        log.setLevel(prev)


def _parse_oauth_token_body(r: httpx.Response) -> dict[str, Any]:
    """GitHub may return JSON or ``application/x-www-form-urlencoded``."""
    try:
        data = r.json()
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    raw = (r.text or "").strip()
    if not raw:
        return {}
    pairs = parse_qs(raw, keep_blank_values=True)
    return {k: (v[0] if v else "") for k, v in pairs.items()}

_DEVICE_FILE = ".agent_github_device.json"
_TOKEN_FILE = ".agent_github_user_token"


def device_state_path(workspace: Path) -> Path:
    return workspace / _DEVICE_FILE


def user_token_path(workspace: Path) -> Path:
    return workspace / _TOKEN_FILE


def request_device_authorization(workspace: Path) -> str:
    """Start device flow; write pending state. Returns user-facing instructions."""
    client_id = (os.getenv("GITHUB_OAUTH_CLIENT_ID") or "").strip()
    if not client_id:
        raise RuntimeError(
            "GITHUB_OAUTH_CLIENT_ID is not set. Create a GitHub **OAuth App** (Developer settings), "
            "enable **Device flow**, set Authorization callback URL to a placeholder if required, "
            "then put the app's Client ID in GITHUB_OAUTH_CLIENT_ID."
        )

    scope = (os.getenv("GITHUB_DEVICE_SCOPE") or "repo").strip()
    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            "https://github.com/login/device/code",
            data={"client_id": client_id, "scope": scope},
            headers={"Accept": "application/json"},
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"GitHub device/code failed: HTTP {r.status_code} {r.text[:800]}"
            )
        data = r.json()
    device_code = str(data.get("device_code") or "").strip()
    if not device_code:
        raise RuntimeError(f"Unexpected device/code response: {data!r}")

    interval = max(5, int(data.get("interval") or 5))
    expires_in = int(data.get("expires_in") or 900)
    user_code = str(data.get("user_code") or "").strip()
    verification_uri = str(
        data.get("verification_uri_complete")
        or data.get("verification_uri")
        or "https://github.com/login/device"
    ).strip()

    state = {
        "device_code": device_code,
        "client_id": client_id,
        "interval": interval,
        "deadline": time.time() + float(expires_in),
    }
    device_state_path(workspace).write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )

    return (
        "GitHub device login started.\n\n"
        f"1) Open: {verification_uri}\n"
        f"2) Enter this code when GitHub asks: **{user_code}**\n"
        f"3) Approve access (scope includes: {scope!r}).\n\n"
        "When finished, ask the agent to run **complete_github_device_login** (or send a short message "
        "so the model calls that tool). Publishing will then use **your** GitHub account, not the server operator’s."
    )


def complete_device_authorization(workspace: Path) -> str:
    """Poll until the user authorizes; save OAuth access token for this workspace."""
    path = device_state_path(workspace)
    if not path.is_file():
        raise RuntimeError(
            "No pending GitHub device login for this workspace. Run **begin_github_device_login** first."
        )
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Corrupt device state file: {e}") from e

    device_code = str(state.get("device_code") or "").strip()
    client_id = str(state.get("client_id") or "").strip()
    deadline = float(state.get("deadline") or 0)

    if not device_code or not client_id:
        path.unlink(missing_ok=True)
        raise RuntimeError("Invalid device state; start again with begin_github_device_login.")

    grant = "urn:ietf:params:oauth:grant-type:device_code"
    poll_interval = max(5, int(state.get("interval") or 5))
    max_polls = max(1, int(os.getenv("GITHUB_DEVICE_MAX_POLLS_PER_CALL", "10")))
    iteration = 0

    with httpx.Client(timeout=30.0) as client, _suppress_httpx_info_logs():
        while time.time() < deadline:
            iteration += 1
            if iteration > max_polls:
                return (
                    "Still waiting for GitHub device authorization (no access token yet).\n\n"
                    "1) Open the **verification URL** from **begin_github_device_login**.\n"
                    "2) Enter the **user code** and click **Authorize** on GitHub.\n"
                    "3) Send **continue** or **authorized** here — I will poll again and finish linking.\n\n"
                    "(Each chat turn polls GitHub only a limited number of times so the conversation "
                    "stays responsive; this is normal until you approve in the browser.)"
                )

            r = client.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": client_id,
                    "device_code": device_code,
                    "grant_type": grant,
                },
                headers={"Accept": "application/json"},
            )
            if r.status_code != 200:
                raise RuntimeError(
                    f"Token poll failed: HTTP {r.status_code} {r.text[:800]}"
                )
            body = _parse_oauth_token_body(r)
            if body.get("access_token"):
                token = str(body["access_token"]).strip()
                if not token:
                    break
                user_token_path(workspace).write_text(token + "\n", encoding="utf-8")
                path.unlink(missing_ok=True)
                uh = client.get(
                    "https://api.github.com/user",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
                login = ""
                if uh.status_code == 200:
                    login = str((uh.json() or {}).get("login") or "").strip()
                who = f"@{login}" if login else "your GitHub user"
                return (
                    f"GitHub linked successfully for {who}. "
                    "You can run **publish_workspace_to_github**; repos will be created under that account."
                )

            err = str(body.get("error") or "").strip()
            if err == "authorization_pending":
                time.sleep(poll_interval)
                continue
            if err == "slow_down":
                poll_interval += 5
                time.sleep(poll_interval)
                continue
            if err == "expired_token" or err == "access_denied":
                path.unlink(missing_ok=True)
                raise RuntimeError(
                    f"GitHub device authorization ended: {err}. Run begin_github_device_login again."
                )
            path.unlink(missing_ok=True)
            raise RuntimeError(
                f"GitHub device flow error: {err or body!r}"
            )

    path.unlink(missing_ok=True)
    raise RuntimeError(
        "Timed out waiting for you to authorize on GitHub (codes expire in ~15 minutes). "
        "Run begin_github_device_login again."
    )
