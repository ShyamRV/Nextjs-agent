"""LangGraph agent pipeline with ASI:One and sandbox tools.

Uses in-memory checkpointing by default (no Postgres required).
Set DATABASE_URL to enable persistent Postgres checkpointing for cross-restart memory.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_openai import ChatOpenAI
from langchain_core.runnables import RunnableConfig

from ai.llm_config import resolve_llm_settings, session_headers
from ai.models import SandboxResponse, UserContext
from ai.session_workspace import get_or_create_workspace, session_workspace_enabled
from ai.tools import tools
from sandbox import preview_registry

logger = logging.getLogger("ai")

_KEEP_WORKSPACES = os.getenv("SANDBOX_KEEP_WORKSPACES", "").lower() in {"1", "true", "yes"}


def _build_llm(session_id: str | None) -> ChatOpenAI:
    s = resolve_llm_settings()
    kwargs: dict[str, Any] = {
        "model": s.model,
        "temperature": 0.3,
    }
    if s.api_key:
        kwargs["api_key"] = s.api_key
    kwargs["base_url"] = s.base_url
    headers = session_headers(session_id)
    if headers:
        kwargs["default_headers"] = headers
    return ChatOpenAI(**kwargs)


def _make_checkpointer() -> Any:
    """Return Postgres checkpointer when DATABASE_URL is set, else in-memory."""
    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if db_url:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            # Caller must call .setup() before use — handled in AI.setup_agent()
            return AsyncPostgresSaver.from_conn_string(db_url)
        except Exception as exc:
            logger.warning("[checkpointer] Postgres unavailable (%s); falling back to memory", exc)

    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.checkpoint.serde._msgpack import SAFE_MSGPACK_TYPES
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    # ToolStrategy stores SandboxResponse in checkpoints; allow msgpack deserialize.
    serde = JsonPlusSerializer(
        allowed_msgpack_modules=set(SAFE_MSGPACK_TYPES) | {("ai.models", "SandboxResponse")},
    )
    return MemorySaver(serde=serde)


def _response_from_state(final_state: dict[str, Any] | None) -> dict[str, str | None]:
    fallback = {"type": "error", "text": "Something went wrong. Please try again."}
    if not final_state:
        return fallback
    raw = final_state.get("structured_response")
    if raw is not None:
        if isinstance(raw, SandboxResponse):
            return {"type": "response", "text": raw.text}
        if isinstance(raw, dict):
            try:
                return {"type": "response", "text": SandboxResponse.model_validate(raw).text}
            except Exception:
                pass
    for msg in reversed(final_state.get("messages") or []):
        if getattr(msg, "content", None) and not getattr(msg, "tool_calls", None):
            text = msg.content if isinstance(msg.content, str) else str(msg.content)
            if text:
                return {"type": "response", "text": text}
    return fallback


class AI:
    """LangGraph agent for Next.js sandbox execution."""

    def __init__(self) -> None:
        self._checkpointer: Any = None

    async def setup_agent(self) -> None:
        s = resolve_llm_settings()
        if not s.api_key:
            raise RuntimeError("ASI_ONE_API_KEY is not set.")
        self._checkpointer = _make_checkpointer()
        # Run Postgres DDL setup if applicable
        if hasattr(self._checkpointer, "setup"):
            try:
                await self._checkpointer.setup()
                logger.info("[checkpointer] Postgres tables ready")
            except Exception as exc:
                logger.warning("[checkpointer] setup() failed: %s", exc)

    def _build_graph(self, session_id: str | None) -> Any:
        prompt_path = Path(__file__).parent / "PROMPT.md"
        return create_agent(
            model=_build_llm(session_id),
            tools=tools,
            checkpointer=self._checkpointer,
            context_schema=UserContext,
            system_prompt=prompt_path.read_text(encoding="utf-8"),
            response_format=ToolStrategy(schema=SandboxResponse),
        )

    async def ask(
        self,
        user_id: str,
        session_id: str,
        question: str,
        logger: logging.Logger,
    ) -> AsyncGenerator[dict[str, str | None], None]:
        root = await get_or_create_workspace(user_id, session_id)
        skip_preview = os.getenv("SKIP_PREVIEW_DEV", "").lower() in {"1", "true", "yes", "on"}
        preview_base = (os.getenv("PREVIEW_PUBLIC_BASE_URL") or "").strip()
        thread_id = f"njss:{user_id}:{session_id}"

        try:
            if not self._checkpointer:
                raise ValueError("Agent not initialized — call setup_agent() first")

            agent = self._build_graph(session_id)
            config: RunnableConfig = {
                "configurable": {"thread_id": thread_id},
                "recursion_limit": 50,
            }

            final_state: dict[str, Any] | None = None
            async for mode, chunk in agent.astream(
                {"messages": [{"role": "user", "content": question}]},
                config,
                context=UserContext(
                    user_id=user_id,
                    session_id=session_id,
                    workspace_dir=str(root),
                    skip_preview=skip_preview,
                    preview_public_base_url=preview_base,
                ),
                stream_mode=["custom", "values"],
            ):
                if mode == "custom":
                    yield {"type": "update", "text": chunk}
                elif mode == "values":
                    final_state = chunk

            out = _response_from_state(final_state)
            logger.info("[ai] final response type=%s", out.get("type"))
            yield out

        except Exception:
            logger.exception("Error processing Next.js sandbox request")
            yield {"type": "error", "text": "Something went wrong. Please try again."}

        finally:
            if not preview_registry.consumes_ask_rmtree(str(root)):
                await preview_registry.shutdown_workspace_dev(str(root))
                if not _KEEP_WORKSPACES and not session_workspace_enabled():
                    shutil.rmtree(root, ignore_errors=True)


# ── Singleton ──────────────────────────────────────────────────────────────────

_ai: AI | None = None


async def setup_ai_instance() -> None:
    global _ai
    if _ai is None:
        _ai = AI()
        await _ai.setup_agent()


def ask(
    user_id: str,
    session_id: str,
    question: str,
    logger: logging.Logger,
) -> AsyncGenerator[dict[str, str | None], None]:
    if _ai is None:
        raise RuntimeError("AI not initialized. Call setup_ai_instance() first.")
    return _ai.ask(user_id, session_id, question, logger)
