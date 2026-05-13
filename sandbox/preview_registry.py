"""Track dev-server subprocesses, remote sandboxes, and deferred workspace cleanup."""

from __future__ import annotations

import asyncio

_dev_by_workspace: dict[str, asyncio.subprocess.Process] = {}
_remote_sandbox_by_workspace: dict[str, object] = {}
_deferred_workspace_delete: set[str] = set()


def register_dev_server(workspace_dir: str, proc: asyncio.subprocess.Process) -> None:
    _dev_by_workspace[workspace_dir] = proc


def register_remote_preview_sandbox(workspace_dir: str, sandbox: object) -> None:
    """Hold a remote sandbox (e.g. E2B ``Sandbox``) for ``kill()`` / ``delete()`` on cleanup."""
    _remote_sandbox_by_workspace[workspace_dir] = sandbox


def mark_deferred_workspace_delete(workspace_dir: str) -> None:
    """Workspace tree will be removed by a background task (do not rmtree in ask() finally)."""
    _deferred_workspace_delete.add(workspace_dir)


def consumes_ask_rmtree(workspace_dir: str) -> bool:
    return workspace_dir in _deferred_workspace_delete


def clear_deferred_marker(workspace_dir: str) -> None:
    _deferred_workspace_delete.discard(workspace_dir)


async def shutdown_workspace_dev(workspace_dir: str) -> None:
    from sandbox.runner import terminate_process

    proc = _dev_by_workspace.pop(workspace_dir, None)
    await terminate_process(proc)

    remote = _remote_sandbox_by_workspace.pop(workspace_dir, None)
    if remote is not None:

        def _stop_remote() -> None:
            for name in ("kill", "delete"):
                fn = getattr(remote, name, None)
                if callable(fn):
                    try:
                        fn()
                        return
                    except Exception:
                        continue

        await asyncio.to_thread(_stop_remote)
