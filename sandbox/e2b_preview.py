"""Run Next.js preview in an E2B cloud sandbox and return a public URL (free tier: E2B Hobby)."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from e2b import Sandbox
from e2b.sandbox.filesystem.filesystem import WriteEntry


def _log(cb: Callable[[str], None] | None, msg: str) -> None:
    if cb:
        cb(msg)


def _iter_upload_files(workspace: Path) -> list[tuple[Path, str]]:
    root = workspace.resolve()
    skip_root_dirs = {
        "node_modules",
        ".next",
        ".git",
        ".venv",
        "__pycache__",
    }
    out: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        rel_parts = Path(dirpath).resolve().relative_to(root).parts
        if rel_parts and rel_parts[0] in skip_root_dirs:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in skip_root_dirs]
        for name in filenames:
            if name == ".agent_build_attempts":
                continue
            abs_path = Path(dirpath) / name
            if not abs_path.is_file():
                continue
            rel = abs_path.resolve().relative_to(root).as_posix()
            out.append((abs_path, rel))
    return out


def _preview_url(sandbox: Sandbox, port: int = 3000) -> str:
    host = sandbox.get_host(port)
    if host.startswith("http://") or host.startswith("https://"):
        return host
    return f"https://{host}"


def deploy_nextjs_preview_to_e2b(
    workspace: Path,
    *,
    on_log: Callable[[str], None] | None = None,
) -> tuple[Sandbox, str, str]:
    """Sync: create E2B sandbox, upload sources, ``npm install`` / ``build`` / ``start``, return public URL.

    Returns:
        (sandbox, preview_url, combined_log). Call ``sandbox.kill()`` when done.

    Raises:
        RuntimeError: missing key, no npm in template, or npm failure.
    """
    api_key = (os.getenv("E2B_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("E2B_API_KEY is not set")

    template = (os.getenv("E2B_SANDBOX_TEMPLATE") or Sandbox.default_template).strip()
    # Hobby tier max sandbox lifetime is 3600s; stay slightly under if unset.
    raw_timeout = int(os.getenv("E2B_SANDBOX_TIMEOUT_SEC", "3500"))
    sandbox_timeout = max(60, min(raw_timeout, 86_400))
    install_timeout = float(os.getenv("E2B_NPM_INSTALL_TIMEOUT_SEC", "600"))
    build_timeout = float(os.getenv("E2B_NPM_BUILD_TIMEOUT_SEC", "600"))
    remote_root = (
        os.getenv("E2B_PROJECT_REMOTE_DIR") or "/home/user/nextjs_sandbox_app"
    ).strip()
    batch = int(os.getenv("E2B_UPLOAD_BATCH", "40"))

    logs: list[str] = []

    def append(msg: str) -> None:
        logs.append(msg)
        _log(on_log, msg)

    append(f"Creating E2B sandbox (template={template!r}, timeout={sandbox_timeout}s)…")
    sandbox = Sandbox.create(
        template=template,
        timeout=sandbox_timeout,
        api_key=api_key,
    )

    try:
        sandbox.files.make_dir(remote_root)

        files = _iter_upload_files(workspace)
        if not files:
            raise RuntimeError("No files to upload from workspace")

        append(f"Uploading {len(files)} file(s)…")
        chunk: list[WriteEntry] = []
        for abs_path, rel in files:
            dest = f"{remote_root.rstrip('/')}/{rel}"
            chunk.append(WriteEntry(path=dest, data=abs_path.read_bytes()))
            if len(chunk) >= batch:
                sandbox.files.write_files(chunk)
                chunk.clear()
        if chunk:
            sandbox.files.write_files(chunk)

        append("Checking Node/npm in sandbox…")
        probe = sandbox.commands.run("command -v npm && npm -v", cwd=remote_root, timeout=30)
        if probe.exit_code != 0:
            raise RuntimeError(
                "This E2B template does not have npm on PATH. Build a Node template with "
                "`e2b template` (see https://e2b.dev/docs/sandbox-templates ) and set "
                "E2B_SANDBOX_TEMPLATE to that template id, or use the default `base` if your "
                "team snapshot already includes Node."
            )

        def run_logged(cmd: str, timeout: float) -> None:
            append(f"$ {cmd}")
            res = sandbox.commands.run(cmd, cwd=remote_root, timeout=timeout)
            tail_out = (res.stdout or "")[-4000:]
            tail_err = (res.stderr or "")[-4000:]
            append(f"exit={res.exit_code}\n--- stdout (tail) ---\n{tail_out}\n--- stderr (tail) ---\n{tail_err}")
            if res.exit_code != 0:
                raise RuntimeError(f"Command failed (exit={res.exit_code}): {cmd[:120]}")

        run_logged("npm install --no-audit --no-fund", install_timeout)
        run_logged("npm run build", build_timeout)

        append("$ npm run start -- --hostname 0.0.0.0 --port 3000 (background)")
        sandbox.commands.run(
            "npm run start -- --hostname 0.0.0.0 --port 3000",
            cwd=remote_root,
            background=True,
            timeout=120,
        )

        url = _preview_url(sandbox, 3000)
        append(f"Preview URL: {url}")
        return sandbox, url, "\n".join(logs)
    except Exception:
        try:
            sandbox.kill()
        except Exception:
            pass
        raise

