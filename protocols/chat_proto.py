"""
Chat protocol for nextjs-sandbox-agent.

Flow: ACK inbound → extract text → (usage / payment gate) → run AI pipeline → stream updates → send final reply.
"""

from __future__ import annotations

import logging
import os

from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    MetadataContent,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)
from uagents_core.contrib.protocols.payment import Funds, RequestPayment

from protocols.payment_proto import payment_recipient_address
from sandbox import usage_limits

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


def _billing_user_key(sender: str, msg: ChatMessage) -> str:
    """Prefer ``email`` / ``user_email`` from ``MetadataContent``; else uAgent ``sender`` address."""
    for item in msg.content or []:
        if isinstance(item, MetadataContent):
            md = item.metadata or {}
            for k in ("email", "user_email", "mail"):
                v = (md.get(k) or "").strip().lower()
                if v and "@" in v:
                    return v
    return (sender or "").strip()


async def _maybe_send_payment_request(ctx: Context, sender: str, billing_key: str) -> None:
    """Send Agent Payment Protocol ``RequestPayment`` (buyer role = this agent)."""
    recipient = payment_recipient_address(ctx)
    if not recipient:
        logger.warning("PAYMENT_RECIPIENT_ADDRESS unset and agent address unavailable; skip RequestPayment")
        return
    try:
        amount = (os.getenv("PAYMENT_PRICE_AMOUNT") or "9.99").strip()
        currency = (os.getenv("PAYMENT_CURRENCY") or "USD").strip()
        method = (os.getenv("PAYMENT_METHOD") or "stripe").strip()
        deadline = int(os.getenv("PAYMENT_DEADLINE_SECONDS") or "86400")
        req = RequestPayment(
            accepted_funds=[Funds(amount=amount, currency=currency, payment_method=method)],
            recipient=recipient,
            deadline_seconds=max(60, deadline),
            reference=billing_key[:2000],
            description="Next.js sandbox agent — continued website generations after free tier",
            metadata={
                "product": "nextjs-sandbox-agent",
                "free_tier_sites": str(usage_limits.get_free_site_quota()),
            },
        )
        await ctx.send(sender, req)
    except Exception as exc:
        logger.warning("Failed to send RequestPayment to %s: %s", sender, exc)


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

    billing_key = _billing_user_key(sender, msg)

    logger.info(
        "Processing: session=%s sender=%s text=%s%s",
        session_id, sender,
        combined_prompt[:120],
        "..." if len(combined_prompt) > 120 else "",
    )

    if not usage_limits.can_start_new_site(billing_key) and not usage_limits.has_payment_entitlement(
        billing_key
    ):
        await _maybe_send_payment_request(ctx, sender, billing_key)
        await _reply(
            ctx,
            sender,
            usage_limits.payment_block_message(billing_key),
        )
        return

    try:
        from ai import ask

        got_final = False
        async for chunk in ask(
            user_id=sender,
            session_id=session_id,
            question=combined_prompt,
            logger=logger,
            billing_user_key=billing_key,
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
