"""Reuse one Next.js workspace per Agentverse chat session for follow-up edits."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from sandbox.scaffold import write_workspace


def session_workspace_enabled() -> bool:
    v = (os.getenv("NEXTJS_SESSION_WORKSPACE") or "true").strip().lower()
    return v not in {"0", "false", "no", "off"}


def _workspace_parent() -> Path:
    base = os.getenv("SANDBOX_WORKSPACE_ROOT") or os.path.join(
        tempfile.gettempdir(), "nextjs_sandbox_agent"
    )
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _key(user_id: str, session_id: str) -> str:
    return f"{user_id}\x1f{session_id}"


_lock = asyncio.Lock()
_roots: dict[str, Path] = {}


async def get_or_create_workspace(user_id: str, session_id: str) -> Path:
    """Return workspace path; reuse per session when enabled and folder still exists."""
    if not session_workspace_enabled():
        root = Path(tempfile.mkdtemp(prefix="nx_", dir=str(_workspace_parent())))
        write_workspace(root)
        return root

    key = _key(user_id, session_id)
    async with _lock:
        existing = _roots.get(key)
        if existing is not None and existing.is_dir() and (existing / "package.json").is_file():
            return existing
        root = Path(tempfile.mkdtemp(prefix="nx_", dir=str(_workspace_parent())))
        write_workspace(root)
        _roots[key] = root
        return root


def forget_workspace(user_id: str, session_id: str) -> None:
    _roots.pop(_key(user_id, session_id), None)
