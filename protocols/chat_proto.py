"""
Chat protocol for nextjs-sandbox-agent.

Flow: ACK inbound → extract text → run AI pipeline → stream updates → send final reply.
"""

from __future__ import annotations

import logging

from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

logger = logging.getLogger("chat_proto")

nextjs_sandbox_chat_proto = Protocol(spec=chat_protocol_spec)


def _build_text_chat(text: str) -> ChatMessage:
    """Construct a ChatMessage with a single TextContent block.

    Matches ``uagents_core`` 0.4.x ``ChatMessage`` (content + optional ids/timestamps only).
    """
    return ChatMessage(
        content=[TextContent(text=text)],
        msg_id=None,
        timestamp=None,
    )


def _extract_text(msg: ChatMessage) -> str:
    parts: list[str] = []
    for item in msg.content or []:
        if isinstance(item, TextContent) and item.text:
            parts.append(item.text)
    return "\n".join(parts).strip()


async def _ack(ctx: Context, sender: str, msg: ChatMessage) -> None:
    try:
        await ctx.send(sender, ChatAcknowledgement(acknowledged_msg_id=msg.msg_id))
    except Exception:
        pass


async def _reply(ctx: Context, sender: str, text: str) -> None:
    try:
        await ctx.send(sender, _build_text_chat(text))
    except Exception as exc:
        logger.warning("Failed to send reply to %s: %s", sender, exc)


@nextjs_sandbox_chat_proto.on_message(ChatMessage)
async def handle_message(ctx: Context, sender: str, msg: ChatMessage) -> None:
    session_id = str(ctx.session)
    msg_id = getattr(msg, "msg_id", None)

    logger.info(
        "ChatMessage session=%s msg_id=%s sender=%s",
        session_id, msg_id, sender,
    )

    await _ack(ctx, sender, msg)

    # Handle session lifecycle events
    for item in msg.content or []:
        if isinstance(item, StartSessionContent):
            logger.info("Session started: %s from %s", session_id, sender)
        elif isinstance(item, EndSessionContent):
            logger.info("Session ended: %s from %s", session_id, sender)

    combined_prompt = _extract_text(msg)
    if not combined_prompt:
        return

    logger.info(
        "Processing: session=%s sender=%s text=%s%s",
        session_id, sender,
        combined_prompt[:120],
        "..." if len(combined_prompt) > 120 else "",
    )

    try:
        from ai import ask

        got_final = False
        async for chunk in ask(
            user_id=sender,
            session_id=session_id,
            question=combined_prompt,
            logger=logger,
        ):
            chunk_type = chunk.get("type")

            if chunk_type == "update":
                logger.info("Update: %s", (chunk.get("text") or "")[:200])

            elif chunk_type in ("response", "error"):
                got_final = True
                text = chunk.get("text") or "Something went wrong. Please try again."
                await _reply(ctx, sender, text)
                break

        if not got_final:
            await _reply(ctx, sender, "Something went wrong. Please try again.")

    except Exception as exc:
        logger.exception("Error processing message session=%s: %s", session_id, exc)
        await _reply(ctx, sender, "Something went wrong while running the sandbox. Please try again.")


@nextjs_sandbox_chat_proto.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
    logger.info("Ack from %s for %s", sender, msg.acknowledged_msg_id)
