"""Next.js sandbox agent — uAgents entrypoint (chat protocol + local npm sandbox)."""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

import dotenv
from uagents import Agent, Context

dotenv.load_dotenv()

# Optional: change working dir if AGENT_STATE_DIR is set
_state_dir = os.getenv("AGENT_STATE_DIR")
if _state_dir:
    Path(_state_dir).mkdir(parents=True, exist_ok=True)
    os.chdir(_state_dir)

from protocols.chat_proto import nextjs_sandbox_chat_proto
from protocols.payment_proto import payment_merchant_proto

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("agent")

_REQUIRED_ENV_VARS = [
    "AGENT_SEED",
    "ASI_ONE_API_KEY",
]


def _validate_env() -> None:
    missing = [v for v in _REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")


def _load_seed() -> str:
    seed = os.getenv("AGENT_SEED")
    if seed:
        return seed
    raise RuntimeError("AGENT_SEED is not set.")


def _tcp_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True if something is already accepting TCP connections on ``host:port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


AGENT_NAME = os.getenv("AGENT_NAME", "nextjs_sandbox_agent")
AGENT_PORT = int(os.getenv("AGENT_PORT", "8029"))
AGENTVERSE_URL = os.getenv("AGENTVERSE_URL", "https://agentverse.ai")

agent = Agent(
    name=AGENT_NAME,
    mailbox=True,
    port=AGENT_PORT,
    seed=_load_seed(),
    handle_messages_concurrently=True,
    agentverse=AGENTVERSE_URL,
    mark_inactive_on_shutdown=False,
    shutdown_timeout=int(os.getenv("SHUTDOWN_TIMEOUT_SECONDS", "60")),
)


@agent.on_event("startup")
async def startup(ctx: Context) -> None:
    _validate_env()

    address = str(ctx.address) if hasattr(ctx, "address") and ctx.address else str(agent.address)
    logger.info("Next.js sandbox agent started: %s", address)

    try:
        from ai import setup_ai_instance
        await setup_ai_instance()
        logger.info("[ai] LangGraph agent initialized")
    except Exception as exc:
        logger.error("[ai] failed to initialize: %s", exc)
        raise


agent.include(nextjs_sandbox_chat_proto, publish_manifest=True)
agent.include(payment_merchant_proto, publish_manifest=True)

if __name__ == "__main__":
    if _tcp_port_in_use(AGENT_PORT):
        raise RuntimeError(
            f"Port {AGENT_PORT} is already in use (WinError 10048 / EADDRINUSE). "
            "Another `python agent.py` or app is bound to it.\n\n"
            "Fix:\n"
            "• Stop the other agent: close its terminal or press Ctrl+C there.\n"
            f"• Or find the process: PowerShell `Get-NetTCPConnection -LocalPort {AGENT_PORT}` "
            "then `Stop-Process -Id <OwningProcess> -Force`.\n"
            f"• Or use a free port: set AGENT_PORT=8030 in .env (update Agentverse / "
            "inspector URL to match)."
        )
    agent.run()
