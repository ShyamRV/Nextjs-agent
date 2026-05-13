"""ASI:One LLM configuration (OpenAI-compatible Chat Completions)."""

from __future__ import annotations

import os
from dataclasses import dataclass

_DEFAULT_BASE = "https://api.asi1.ai/v1"
_DEFAULT_MODEL = "asi1"


@dataclass(frozen=True, slots=True)
class LLMSettings:
    api_key: str | None
    base_url: str
    model: str
    use_session_header: bool


def resolve_llm_settings() -> LLMSettings:
    api_key = os.getenv("ASI_ONE_API_KEY", "").strip() or None
    base_url = (os.getenv("ASI_ONE_BASE_URL") or _DEFAULT_BASE).strip()
    model = (os.getenv("ASI_ONE_MODEL") or _DEFAULT_MODEL).strip()
    use_session = _env_bool("ASI1_USE_SESSION_HEADER", default=True)
    return LLMSettings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        use_session_header=use_session,
    )


def session_headers(session_id: str | None = None) -> dict[str, str]:
    """Optional x-session-id header for ASI:One routing."""
    s = resolve_llm_settings()
    if s.api_key and s.use_session_header and session_id:
        return {"x-session-id": session_id}
    return {}


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}
