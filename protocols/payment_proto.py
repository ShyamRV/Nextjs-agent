"""Fetch.ai **Agent Payment Protocol** (uAgents) — merchant + buyer flows on one agent.

The stock ``payment_protocol_spec`` splits **buyer** vs **seller** roles in a way that
prevents registering both ``RequestPayment`` (outbound) and ``CommitPayment`` (inbound)
on a single ``Protocol`` instance. We use a spec **copy with ``roles=None``** so every
interaction model is available, matching the Innovation Lab semantics while staying
compatible with ``uagents`` 0.24.x.

See: https://fetch.ai (Agent Payment Protocol documentation)
"""

from __future__ import annotations

import logging
import os

from uagents import Context, Protocol
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    RejectPayment,
    RequestPayment,
    payment_protocol_spec as _canonical_payment_spec,
)
from uagents_core.protocol import ProtocolSpecification

from sandbox import usage_limits

logger = logging.getLogger("payment_proto")

# Full interaction map, no role lock-out (single agent is both initiator and responder).
_merchant_payment_spec = ProtocolSpecification(
    name=_canonical_payment_spec.name,
    version=_canonical_payment_spec.version,
    interactions=dict(_canonical_payment_spec.interactions),
    roles=None,
)

payment_merchant_proto = Protocol(spec=_merchant_payment_spec)


def payment_recipient_address(ctx: Context) -> str:
    """Where funds should be sent (operator-configured or this agent's address)."""
    explicit = (os.getenv("PAYMENT_RECIPIENT_ADDRESS") or "").strip()
    if explicit:
        return explicit
    try:
        return str(ctx.agent.address)
    except Exception:
        return ""


def billing_key_from_commit(msg: CommitPayment) -> str | None:
    ref = (msg.reference or "").strip()
    return ref or None


@payment_merchant_proto.on_message(CommitPayment)
async def on_commit_payment(ctx: Context, sender: str, msg: CommitPayment) -> None:
    logger.info("CommitPayment from %s txn=%s", sender, msg.transaction_id)
    billing_key = billing_key_from_commit(msg) or sender
    usage_limits.record_payment_entitlement(billing_key, msg.transaction_id)
    try:
        await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
    except Exception as exc:
        logger.warning("Failed to send CompletePayment: %s", exc)


@payment_merchant_proto.on_message(RejectPayment)
async def on_reject_payment(ctx: Context, sender: str, msg: RejectPayment) -> None:
    logger.info("RejectPayment from %s reason=%s", sender, getattr(msg, "reason", None))


@payment_merchant_proto.on_message(RequestPayment)
async def on_request_payment(ctx: Context, sender: str, msg: RequestPayment) -> None:
    """Unexpected inbound request (we normally send RequestPayment outbound from chat)."""
    logger.info("RequestPayment received from %s (ignored on merchant agent)", sender)


@payment_merchant_proto.on_message(CompletePayment)
async def on_complete_payment(ctx: Context, sender: str, msg: CompletePayment) -> None:
    logger.info("CompletePayment from %s txn=%s", sender, getattr(msg, "transaction_id", None))


@payment_merchant_proto.on_message(CancelPayment)
async def on_cancel_payment(ctx: Context, sender: str, msg: CancelPayment) -> None:
    logger.info("CancelPayment from %s txn=%s", sender, getattr(msg, "transaction_id", None))
