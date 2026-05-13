"""Per-user website generation limits and payment entitlement (persistent JSON)."""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()

_DEFAULT_FREE = 3


def _free_generations() -> int:
    try:
        return max(0, int(os.getenv("SITE_GENERATIONS_FREE") or str(_DEFAULT_FREE)))
    except ValueError:
        return _DEFAULT_FREE


def _usage_path() -> Path:
    base = (os.getenv("AGENT_STATE_DIR") or "").strip() or os.getcwd()
    return Path(base) / ".agent_usage.json"


def _bypass() -> bool:
    return (os.getenv("PAYMENT_BYPASS") or "").strip().lower() in {"1", "true", "yes", "on"}


def _encode_key(billing_user_key: str) -> str:
    raw = billing_user_key.strip().encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _load() -> dict[str, Any]:
    path = _usage_path()
    if not path.is_file():
        return {"by_key": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"by_key": {}}
    except (json.JSONDecodeError, OSError):
        return {"by_key": {}}


def _save(data: dict[str, Any]) -> None:
    path = _usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _bucket(data: dict[str, Any], billing_user_key: str) -> dict[str, Any]:
    by = data.setdefault("by_key", {})
    if not isinstance(by, dict):
        data["by_key"] = {}
        by = data["by_key"]
    kid = _encode_key(billing_user_key)
    b = by.get(kid)
    if not isinstance(b, dict):
        b = {"generations": 0, "entitled": False, "last_txn": None, "updated_at": 0.0}
        by[kid] = b
    return b


def get_generation_count(billing_user_key: str) -> int:
    with _lock:
        b = _bucket(_load(), billing_user_key)
        return int(b.get("generations") or 0)


def has_payment_entitlement(billing_user_key: str) -> bool:
    if _bypass():
        return True
    with _lock:
        b = _bucket(_load(), billing_user_key)
        return bool(b.get("entitled"))


def can_start_new_site(billing_user_key: str) -> bool:
    """True if the user may run another preview / publish (under free tier or entitled)."""
    if _bypass():
        return True
    free = _free_generations()
    with _lock:
        b = _bucket(_load(), billing_user_key)
        if bool(b.get("entitled")):
            return True
        return int(b.get("generations") or 0) < free


def record_successful_site_generation(billing_user_key: str) -> int:
    """Increment after a successful preview. Returns new total generations."""
    if _bypass():
        return 0
    with _lock:
        data = _load()
        b = _bucket(data, billing_user_key)
        n = int(b.get("generations") or 0) + 1
        b["generations"] = n
        b["updated_at"] = time.time()
        _save(data)
        return n


def record_payment_entitlement(billing_user_key: str, transaction_id: str) -> None:
    """Mark user as paid after a valid CommitPayment (or operator override)."""
    with _lock:
        data = _load()
        b = _bucket(data, billing_user_key)
        b["entitled"] = True
        b["last_txn"] = (transaction_id or "")[:512]
        b["updated_at"] = time.time()
        _save(data)


def get_free_site_quota() -> int:
    """Number of successful previews included before payment is required."""
    return _free_generations()


def payment_block_message(_billing_user_key: str, free: int | None = None) -> str:
    lim = free if free is not None else get_free_site_quota()
    return (
        f"You have used all **{lim}** free website previews included for your account.\n\n"
        "A **RequestPayment** message was sent on the **Agent Payment Protocol** (check your wallet / "
        "client agent). After you **CommitPayment**, you can continue.\n\n"
        "If your client does not support the payment protocol yet, ask the operator to grant access "
        "or set `PAYMENT_BYPASS=true` on the agent host for testing."
    )
