"""Pydantic models and runtime context for the Next.js sandbox agent."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class UserContext:
    """Per-request context injected into tools."""
    user_id: str
    session_id: str
    workspace_dir: str
    skip_preview: bool
    preview_public_base_url: str
    billing_user_key: str


class SandboxResponse(BaseModel):
    """Structured final reply the LLM must always emit."""
    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        description=(
            "User-facing report. Must include: "
            "Status (success/failure), Summary, Preview URL (or reason none), "
            "GitHub URL when publish succeeded, and truncated logs when relevant."
        )
    )


class SessionStage(str, Enum):
    """Tracks where in the pipeline a session currently is."""

    BUILDING = "building"  # generating + npm build
    PREVIEW = "preview"  # preview URL sent, awaiting user approval
    APPROVED = "approved"  # user said yes — proceed to deploy
    REJECTED = "rejected"  # user said no — go back to editing
    DEPLOYING = "deploying"  # GitHub + Vercel in progress
    DONE = "done"  # links returned to user


class ApprovalState(BaseModel):
    """Persisted per (user_id, session_id) in memory."""

    model_config = ConfigDict(extra="forbid")

    stage: SessionStage = SessionStage.BUILDING
    preview_url: str = ""
    github_url: str = ""
    vercel_url: str = ""
    workspace_dir: str = ""


# Module-level approval state store
_approval_store: dict[str, ApprovalState] = {}


def get_approval_state(user_id: str, session_id: str) -> ApprovalState:
    key = f"{user_id}:{session_id}"
    if key not in _approval_store:
        _approval_store[key] = ApprovalState()
    return _approval_store[key]


def set_approval_state(user_id: str, session_id: str, state: ApprovalState) -> None:
    _approval_store[f"{user_id}:{session_id}"] = state
