"""LangChain tools for Next.js sandbox: write files, npm install/build, preview, GitHub."""

from __future__ import annotations

import asyncio
import json
import os
import time
from logging import getLogger
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime, tool

from ai.models import (
    SessionStage,
    UserContext,
    get_approval_state,
    set_approval_state,
)
from ai.session_workspace import session_workspace_enabled
from sandbox import preview_registry
from sandbox.github_device import (
    complete_device_authorization,
    request_device_authorization,
)
from sandbox.github_publish import publish_workspace_to_github as github_publish_sync
from sandbox.vercel_deploy import deploy_to_vercel
from sandbox.e2b_preview import deploy_nextjs_preview_to_e2b
from sandbox.runner import (
    pick_free_port,
    run_npm,
    start_next_preview_server,
    wait_for_http_ok,
)

logger = getLogger(__name__)

_MAX_BUILD_RETRIES = int(os.getenv("SANDBOX_BUILD_MAX_RETRIES", "5"))
_INSTALL_TIMEOUT = float(os.getenv("NPM_INSTALL_TIMEOUT_SEC", "600"))
_BUILD_TIMEOUT = float(os.getenv("NPM_BUILD_TIMEOUT_SEC", "600"))
_PREVIEW_KEEPALIVE_SEC = float(os.getenv("PREVIEW_KEEPALIVE_SEC", "600"))
_WAIT_INSTALL_SEC = float(os.getenv("NPM_WAIT_FOR_INSTALL_SEC", "600"))

# Per-workspace lock so install and build never interleave
_npm_locks: dict[str, asyncio.Lock] = {}
# Serialize GitHub device flow (concurrent chat messages must not corrupt device state)
_github_device_locks: dict[str, asyncio.Lock] = {}


def _npm_lock(workspace_key: str) -> asyncio.Lock:
    if workspace_key not in _npm_locks:
        _npm_locks[workspace_key] = asyncio.Lock()
    return _npm_locks[workspace_key]


def _github_device_lock(workspace_key: str) -> asyncio.Lock:
    if workspace_key not in _github_device_locks:
        _github_device_locks[workspace_key] = asyncio.Lock()
    return _github_device_locks[workspace_key]


def _root(runtime: ToolRuntime[UserContext, dict[str, Any]]) -> Path:
    return Path(runtime.context.workspace_dir).resolve()


def _keep_on_disk() -> bool:
    if os.getenv("SANDBOX_KEEP_WORKSPACES", "").lower() in {"1", "true", "yes"}:
        return True
    return session_workspace_enabled()


def _attempts_path(root: Path) -> Path:
    return root / ".agent_build_attempts"


def _read_attempts(root: Path) -> int:
    p = _attempts_path(root)
    if not p.is_file():
        return 0
    try:
        return max(0, int(p.read_text().strip() or "0"))
    except ValueError:
        return 0


def _write_attempts(root: Path, n: int) -> None:
    _attempts_path(root).write_text(str(n))


def _e2b_enabled() -> bool:
    if not (os.getenv("E2B_API_KEY") or "").strip():
        return False
    flag = (os.getenv("PREVIEW_USE_E2B") or "").strip().lower()
    return flag not in ("0", "false", "no", "off")


def _normalize_rel(path: str) -> str:
    rel = path.replace("\\", "/").strip().lstrip("/")
    if ".." in rel.split("/"):
        raise ValueError("Invalid path")
    return rel


def _is_allowed(rel: str) -> bool:
    if rel.startswith("..") or rel.startswith("node_modules/"):
        return False
    if rel in ("package.json", "middleware.ts", "auth.ts", "auth.config.ts",
               "tailwind.config.ts", "postcss.config.mjs", "next.config.mjs",
               "tsconfig.json", "next-env.d.ts", ".eslintrc.json", "README.md"):
        return True
    return any(rel.startswith(p) for p in ("app/", "components/", "lib/", "public/"))


def _validate_package_json(content: str) -> str | None:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        return f"package.json must be valid JSON: {e}"
    if not isinstance(data, dict):
        return "package.json root must be a JSON object"
    return None


# ── File tools ─────────────────────────────────────────────────────────────────

@tool
async def write_app_page_tsx(
    full_source: str,
    runtime: ToolRuntime[UserContext, dict[str, Any]],
) -> str:
    """Write the full contents of app/page.tsx (Next.js App Router + TSX + Tailwind).

    Args:
        full_source: Complete TSX source for the default page export.
    """
    write = runtime.stream_writer
    root = _root(runtime)
    write("Writing app/page.tsx...")
    target = root / "app" / "page.tsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(full_source, encoding="utf-8")
    write("app/page.tsx updated.")
    return f"OK: wrote app/page.tsx ({len(full_source)} bytes)"


@tool
async def write_project_file(
    relative_path: str,
    full_source: str,
    runtime: ToolRuntime[UserContext, dict[str, Any]],
) -> str:
    """Write or overwrite a single project file (allowlisted paths only).

    Allowed: app/**, components/**, lib/**, public/**, root config files, package.json.

    Args:
        relative_path: Path relative to project root (e.g. components/Header.tsx).
        full_source: Full new file contents.
    """
    write = runtime.stream_writer
    try:
        rel = _normalize_rel(relative_path)
    except ValueError:
        return f"Refused: invalid path: {relative_path}"
    if not _is_allowed(rel):
        return f"Refused: path not in allowlist: {relative_path}"
    if rel == "package.json":
        err = _validate_package_json(full_source)
        if err:
            return err
    root = _root(runtime)
    write(f"Writing {rel}...")
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(full_source, encoding="utf-8")
    write(f"Updated {rel}.")
    return f"OK: wrote {rel} ({len(full_source)} bytes)"


@tool
async def apply_json_file_patches(
    patches_json: str,
    runtime: ToolRuntime[UserContext, dict[str, Any]],
) -> str:
    """Apply multiple file updates from a JSON object mapping relative path -> full source string.

    Args:
        patches_json: JSON object like {\"app/page.tsx\": \"...full source...\"}
    """
    write = runtime.stream_writer
    root = _root(runtime)
    try:
        data = json.loads(patches_json)
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {e}"
    if not isinstance(data, dict):
        return "JSON root must be an object"
    written: list[str] = []
    for k, v in data.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        try:
            rel = _normalize_rel(k)
        except ValueError:
            return f"Refused: invalid path: {k}"
        if not _is_allowed(rel):
            return f"Refused: path not in allowlist: {k}"
        if rel == "package.json":
            err = _validate_package_json(v)
            if err:
                return err
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(v, encoding="utf-8")
        written.append(rel)
    write(f"Patched {len(written)} file(s).")
    return f"OK: wrote {written}"


@tool
async def read_sandbox_file(
    relative_path: str,
    runtime: ToolRuntime[UserContext, dict[str, Any]],
) -> str:
    """Read a text file from the sandbox for debugging (truncated at 12 000 chars).

    Args:
        relative_path: Allowlisted path relative to project root.
    """
    write = runtime.stream_writer
    try:
        rel = _normalize_rel(relative_path)
    except ValueError:
        return f"Refused: invalid path: {relative_path}"
    if not _is_allowed(rel):
        return f"Refused: path not in allowlist: {relative_path}"
    root = _root(runtime)
    path = root / rel
    if not path.is_file():
        return f"(missing) {rel}"
    write(f"Reading {rel}...")
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > 12000:
        text = text[:12000] + "\n... [truncated]"
    return text


# ── npm tools ──────────────────────────────────────────────────────────────────

@tool
async def run_npm_install(
    runtime: ToolRuntime[UserContext, dict[str, Any]],
) -> str:
    """Run npm install in the sandbox workspace. Required before the first build.

    Serialized per workspace so install and build never interleave.
    """
    write = runtime.stream_writer
    root = _root(runtime)
    lock = _npm_lock(str(root))
    write("Running npm install...")
    async with lock:
        code, out, err = await run_npm(root, "install", "--no-audit", "--no-fund", timeout_sec=_INSTALL_TIMEOUT)
    write("npm install finished.")
    return f"exit={code}\n--- stdout ---\n{out[-6000:]}\n--- stderr ---\n{err[-6000:]}"


@tool
async def run_npm_build(
    runtime: ToolRuntime[UserContext, dict[str, Any]],
) -> str:
    """Run npm run build. Waits for npm install if running in parallel (serialized per workspace).

    On failure: inspect logs, patch files, re-run. Max SANDBOX_BUILD_MAX_RETRIES attempts.
    """
    write = runtime.stream_writer
    root = _root(runtime)
    lock = _npm_lock(str(root))
    deadline = time.monotonic() + _WAIT_INSTALL_SEC
    notified = False

    while time.monotonic() < deadline:
        async with lock:
            if (root / "node_modules" / "next").is_dir():
                n = _read_attempts(root)
                if n >= _MAX_BUILD_RETRIES:
                    msg = (
                        f"Build attempt limit reached ({_MAX_BUILD_RETRIES}). "
                        "Stop retrying; tell the user what failed."
                    )
                    write(msg)
                    return msg
                _write_attempts(root, n + 1)
                write(f"Running npm run build (attempt {n + 1}/{_MAX_BUILD_RETRIES})...")
                code, out, err = await run_npm(root, "run", "build", timeout_sec=_BUILD_TIMEOUT)
                write("npm run build finished.")
                return f"exit={code}\n--- stdout ---\n{out[-8000:]}\n--- stderr ---\n{err[-8000:]}"
        if not notified:
            write("Waiting for npm install to finish before building...")
            notified = True
        await asyncio.sleep(0.4)

    return (
        "Refused: timed out waiting for node_modules/next. "
        "Ensure run_npm_install completed successfully."
    )


# ── Preview tool ───────────────────────────────────────────────────────────────

@tool
async def start_dev_preview(
    runtime: ToolRuntime[UserContext, dict[str, Any]],
) -> str:
    """After a successful next build, start a preview server and return its URL.

    Uses E2B cloud sandbox (public HTTPS URL) when E2B_API_KEY is set.
    Falls back to local next start (production) on a free port.
    """
    write = runtime.stream_writer
    ctx = runtime.context

    if ctx.skip_preview:
        write("Preview skipped by configuration.")
        return "Preview disabled (SKIP_PREVIEW_DEV=true)."

    root = _root(runtime)
    use_dev = os.getenv("PREVIEW_USE_NEXT_DEV", "").lower() in {"1", "true", "yes", "on"}

    if not use_dev and not (root / ".next").is_dir():
        return "Refused: .next missing — run npm run build successfully first."

    # ── E2B remote preview ────────────────────────────────────────────────────
    if _e2b_enabled():
        loop = asyncio.get_running_loop()

        def _emit(msg: str) -> None:
            loop.call_soon_threadsafe(write, msg)

        write("Starting E2B cloud preview...")
        try:
            sandbox, url, remote_log = await asyncio.to_thread(
                deploy_nextjs_preview_to_e2b, root, on_log=_emit
            )
        except Exception as e:
            return f"E2B preview failed: {e}"

        preview_registry.register_remote_preview_sandbox(str(root), sandbox)
        ok = await wait_for_http_ok(url, timeout_sec=60.0)
        if not ok:
            write("E2B preview URL not yet ready; try refreshing in a few seconds.")

        async def _cleanup() -> None:
            try:
                await asyncio.sleep(_PREVIEW_KEEPALIVE_SEC)
            finally:
                await preview_registry.shutdown_workspace_dev(str(root))
                if not _keep_on_disk():
                    import shutil
                    shutil.rmtree(root, ignore_errors=True)
                preview_registry.clear_deferred_marker(str(root))

        preview_registry.mark_deferred_workspace_delete(str(root))
        asyncio.create_task(_cleanup())
        write("E2B preview ready.")
        return "\n".join([
            url,
            "",
            "This is a public E2B sandbox URL — works from any device (not 127.0.0.1).",
            f"Auto-stops after {int(_PREVIEW_KEEPALIVE_SEC)}s.",
            "",
            "--- E2B log ---",
            remote_log[-12000:],
        ])

    # ── Local preview ─────────────────────────────────────────────────────────
    detach = os.getenv("PREVIEW_DETACH_FROM_AGENT", "").lower() in {"1", "true", "yes", "on"}
    port = pick_free_port()
    mode = "next dev" if use_dev else "next start (production)"
    write(f"Starting {mode} on port {port}...")
    proc = await start_next_preview_server(root, port, use_dev=use_dev, detach=detach)
    preview_registry.register_dev_server(str(root), proc)

    await asyncio.sleep(1.0)
    local_url = f"http://127.0.0.1:{port}"
    ok = await wait_for_http_ok(local_url, timeout_sec=40.0)

    public_base = (ctx.preview_public_base_url or "").rstrip("/")
    url = f"{public_base}:{port}" if ok and public_base else local_url
    if not ok:
        url += " (readiness probe did not pass; server may still be starting)"

    async def _cleanup() -> None:
        try:
            await asyncio.sleep(_PREVIEW_KEEPALIVE_SEC)
        finally:
            await preview_registry.shutdown_workspace_dev(str(root))
            if not _keep_on_disk():
                import shutil
                shutil.rmtree(root, ignore_errors=True)
            preview_registry.clear_deferred_marker(str(root))

    preview_registry.mark_deferred_workspace_delete(str(root))
    asyncio.create_task(_cleanup())
    write("Preview URL ready.")
    return "\n".join([
        url,
        "",
        "Open on the same machine running this agent (127.0.0.1 = loopback only).",
        f"Preview auto-stops after {int(_PREVIEW_KEEPALIVE_SEC)}s.",
    ])


# ── GitHub tools ───────────────────────────────────────────────────────────────

@tool
async def begin_github_device_login(
    runtime: ToolRuntime[UserContext, dict[str, Any]],
) -> str:
    """Start GitHub OAuth device flow so the chat user's GitHub account is used for publish.

    Requires GITHUB_OAUTH_CLIENT_ID (GitHub OAuth App with device flow enabled).
    Returns a user code and URL to open in a browser. After approving, call complete_github_device_login.
    """
    write = runtime.stream_writer
    root = _root(runtime)
    lock = _github_device_lock(str(root))
    write("Starting GitHub device login...")
    async with lock:
        try:
            msg = await asyncio.to_thread(request_device_authorization, root)
        except Exception as e:
            return f"begin_github_device_login failed: {e}"
    write("Device code issued.")
    return msg


@tool
async def complete_github_device_login(
    runtime: ToolRuntime[UserContext, dict[str, Any]],
) -> str:
    """Poll GitHub until the user authorizes; save their OAuth token for this workspace.

    Call only AFTER the user has completed the browser step from begin_github_device_login.
    """
    write = runtime.stream_writer
    root = _root(runtime)
    lock = _github_device_lock(str(root))
    write("Polling GitHub for authorization...")
    async with lock:
        try:
            msg = await asyncio.to_thread(complete_device_authorization, root)
        except Exception as e:
            return f"complete_github_device_login failed: {e}"
    if "Still waiting for GitHub device authorization" in msg:
        write("GitHub authorization still pending (user action needed in browser).")
    else:
        write("GitHub user linked.")
    return msg


@tool
async def publish_workspace_to_github(
    repo_name: str,
    description: str,
    private: bool,
    organization: str,
    runtime: ToolRuntime[UserContext, dict[str, Any]],
) -> str:
    """Create a GitHub repository and push the current workspace source.

    Uses the user-linked OAuth token (from complete_github_device_login) when present,
    else falls back to GITHUB_TOKEN on the agent host (operator bot account).
    If the repo already exists under the same owner, pushes updates instead.

    **Fast path:** When the server has ``GITHUB_TOKEN`` set and the user only wants the
    project published (not necessarily under *their* OAuth-linked account), you may
    call this tool **without** running the device flow first.

    Args:
        repo_name: Repository slug (letters, digits, ., _, - only).
        description: Short GitHub description.
        private: True for private repo.
        organization: GitHub org login, or empty string for the authenticated user's account.
    """
    write = runtime.stream_writer
    root = _root(runtime)
    org = organization.strip() or None
    write("Publishing workspace to GitHub...")
    try:
        url = await asyncio.to_thread(
            github_publish_sync,
            root, repo_name, description,
            private=bool(private),
            organization=org,
        )
    except Exception as e:
        return f"GitHub publish failed: {e}"
    write("GitHub publish finished.")
    return (
        f"OK: repository created/updated and code pushed.\n"
        f"GitHub URL: {url}\n"
        "Remind the user not to commit real API keys to the repo."
    )


# ── Deploy (GitHub + Vercel) ───────────────────────────────────────────────────


@tool
async def deploy_to_github_and_vercel(
    repo_name: str,
    description: str,
    private: bool,
    organization: str,
    vercel_project_name: str,
    runtime: ToolRuntime[UserContext, dict[str, Any]],
) -> str:
    """Full deploy pipeline: push workspace to GitHub, then deploy to Vercel.

    Call this ONLY after the user has approved the preview.

    Steps:
    1. publish_workspace_to_github() → github_url
    2. deploy_to_vercel() → vercel_url
    3. Return both URLs.

    Args:
        repo_name: GitHub repo slug.
        description: Short description for GitHub repo.
        private: True for private GitHub repo.
        organization: GitHub org slug or "" for user account.
        vercel_project_name: Vercel project name (slug, lowercase, hyphens OK).
    """
    write = runtime.stream_writer
    root = _root(runtime)
    ctx = runtime.context
    org = organization.strip() or None

    st0 = get_approval_state(ctx.user_id, ctx.session_id)
    set_approval_state(
        ctx.user_id,
        ctx.session_id,
        st0.model_copy(update={"stage": SessionStage.DEPLOYING, "workspace_dir": str(root)}),
    )

    write("Publishing workspace to GitHub...")
    try:
        github_url = await asyncio.to_thread(
            github_publish_sync,
            root,
            repo_name,
            description,
            private=bool(private),
            organization=org,
        )
    except Exception as e:
        st_bad = get_approval_state(ctx.user_id, ctx.session_id)
        set_approval_state(
            ctx.user_id,
            ctx.session_id,
            st_bad.model_copy(update={"stage": SessionStage.APPROVED}),
        )
        return f"Deploy failed at GitHub: {e}"

    write("GitHub publish finished.")

    vercel_tok = (os.getenv("VERCEL_TOKEN") or "").strip()
    if not vercel_tok:
        st_done = get_approval_state(ctx.user_id, ctx.session_id)
        set_approval_state(
            ctx.user_id,
            ctx.session_id,
            st_done.model_copy(
                update={
                    "stage": SessionStage.DONE,
                    "github_url": github_url,
                    "vercel_url": "",
                }
            ),
        )
        return (
            "OK: GitHub publish succeeded; Vercel skipped (VERCEL_TOKEN not set).\n"
            f"GitHub URL: {github_url}\n"
            "Set VERCEL_TOKEN to enable Vercel deployment (REST API)."
        )

    write("Deploying to Vercel (REST API)...")
    try:

        def _vlog(msg: str) -> None:
            write(msg)

        vercel_url = await deploy_to_vercel(
            root,
            vercel_project_name,
            vercel_token=vercel_tok,
            team_id=None,
            on_log=_vlog,
        )
    except Exception as e:
        st_partial = get_approval_state(ctx.user_id, ctx.session_id)
        set_approval_state(
            ctx.user_id,
            ctx.session_id,
            st_partial.model_copy(
                update={
                    "stage": SessionStage.APPROVED,
                    "github_url": github_url,
                    "vercel_url": "",
                }
            ),
        )
        return (
            f"GitHub publish succeeded but Vercel deploy failed: {e}\n"
            f"GitHub URL: {github_url}"
        )

    st_ok = get_approval_state(ctx.user_id, ctx.session_id)
    set_approval_state(
        ctx.user_id,
        ctx.session_id,
        st_ok.model_copy(
            update={
                "stage": SessionStage.DONE,
                "github_url": github_url,
                "vercel_url": vercel_url,
            }
        ),
    )
    write("Vercel deploy finished.")
    return (
        "OK: deployed successfully.\n"
        f"GitHub URL: {github_url}\n"
        f"Vercel URL: {vercel_url}\n"
        "Remind the user not to commit real API keys to the repo."
    )


@tool
async def signal_preview_approval(
    approved: bool,
    runtime: ToolRuntime[UserContext, dict[str, Any]],
) -> str:
    """Record whether the user approved or rejected the preview.

    Call this when the user replies to the preview URL with approval/rejection.

    Args:
        approved: True if user approved the preview; False if rejected.
    """
    ctx = runtime.context
    st = get_approval_state(ctx.user_id, ctx.session_id)
    if approved:
        set_approval_state(
            ctx.user_id,
            ctx.session_id,
            st.model_copy(update={"stage": SessionStage.APPROVED}),
        )
        return "Preview approved. Proceeding to deploy."
    set_approval_state(
        ctx.user_id,
        ctx.session_id,
        st.model_copy(update={"stage": SessionStage.REJECTED}),
    )
    return "Preview rejected. Ready for edits."


# ── Tool registry ──────────────────────────────────────────────────────────────

tools = [
    write_app_page_tsx,
    write_project_file,
    apply_json_file_patches,
    read_sandbox_file,
    run_npm_install,
    run_npm_build,
    start_dev_preview,
    begin_github_device_login,
    complete_github_device_login,
    publish_workspace_to_github,
    deploy_to_github_and_vercel,
    signal_preview_approval,
]
