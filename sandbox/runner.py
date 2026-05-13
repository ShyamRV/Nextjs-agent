"""Subprocess helpers: npm install/build/dev and free TCP port."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin


def resolve_npm_executable() -> str:
    """Path to npm for subprocess.

    On Windows, ``CreateProcess`` does not run ``npm`` batch shims; prefer ``npm.cmd``
    or the full path from ``shutil.which``.
    """
    for name in ("npm", "npm.cmd"):
        path = shutil.which(name)
        if path:
            return path
    hint = " Install Node.js and ensure npm is on PATH, then restart this process."
    raise FileNotFoundError("npm not found on PATH." + hint)


def _npm_env(cwd: Path) -> dict[str, str]:
    """Ensure local CLI bins (``next``) are on PATH for npm-run scripts."""
    env = {**os.environ, "CI": "true"}
    local_bin = str((cwd / "node_modules" / ".bin").resolve())
    path = env.get("PATH", "")
    if local_bin not in path:
        env["PATH"] = f"{local_bin}{os.pathsep}{path}" if path else local_bin
    return env


async def _stream_reader(stream: asyncio.StreamReader | None, sink: list[str]) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            break
        try:
            sink.append(line.decode(errors="replace"))
        except Exception:
            sink.append(str(line))


async def run_npm(
    cwd: Path,
    *args: str,
    timeout_sec: float | None = None,
) -> tuple[int, str, str]:
    """Run npm with args; return (exit_code, stdout, stderr) as joined strings."""
    npm_exe = resolve_npm_executable()
    cmd = (npm_exe,) + args
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_npm_env(cwd),
    )
    out_chunks: list[str] = []
    err_chunks: list[str] = []
    assert proc.stdout and proc.stderr
    try:
        await asyncio.wait_for(
            asyncio.gather(
                _stream_reader(proc.stdout, out_chunks),
                _stream_reader(proc.stderr, err_chunks),
            ),
            timeout=timeout_sec or 600,
        )
        code = await asyncio.wait_for(proc.wait(), timeout=120)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await asyncio.sleep(0.1)
        code = proc.returncode if proc.returncode is not None else 124
        err_chunks.append("\n[npm] killed after timeout\n")
        return int(code), "".join(out_chunks), "".join(err_chunks)
    return int(code), "".join(out_chunks), "".join(err_chunks)


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return int(s.getsockname()[1])


def _http_get_looks_healthy(r: Any) -> bool:
    """True when status is 2xx and body does not look like an error page."""
    try:
        status = int(getattr(r, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return False
    if not (200 <= status < 300):
        return False
    text = (getattr(r, "text", None) or "").lower()
    if "internal server error" in text:
        return False
    if "application error" in text and "next" in text:
        return False
    if "missing required error components" in text:
        return False
    return True


def _static_asset_urls_from_html(html: str, page_url: str) -> list[str]:
    """Collect a few ``/_next/static/...`` URLs the browser will request after HTML."""
    base = page_url.split("#", 1)[0].rstrip("/") or page_url
    seen: set[str] = set()
    out: list[str] = []
    for m in re.finditer(
        r'(?:src|href)="(/_next/static/(?:chunks|css)/[^"?#]+)"',
        html,
        flags=re.IGNORECASE,
    ):
        path = m.group(1)
        absolute = urljoin(base + "/", path)
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
        if len(out) >= 5:
            break
    return out


async def _static_assets_ok(client: Any, html: str, page_url: str) -> bool:
    """True if main static JS/CSS referenced by the document return 2xx."""
    urls = _static_asset_urls_from_html(html, page_url)
    if not urls:
        return True
    for u in urls:
        try:
            ar = await client.get(
                u,
                headers={
                    "Accept": "*/*",
                    "Accept-Encoding": "gzip, deflate, br",
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            )
        except Exception:
            return False
        if not (200 <= ar.status_code < 300):
            return False
        ct = (ar.headers.get("content-type") or "").lower()
        if "text/html" in ct:
            body = (getattr(ar, "text", None) or "").lower()
            if "internal server error" in body:
                return False
    return True


async def wait_for_http_ok(url: str, *, timeout_sec: float = 20.0) -> bool:
    """Wait until ``GET /`` and key ``/_next/static`` assets behave like a real browser.

    Retries on 5xx or on 200 HTML that still contains an error page. Requires **two**
    consecutive good document GETs, then verifies referenced static chunks/CSS return
    200 (browsers load these immediately; failures often show as a blank or error page
    even when ``curl /`` returns HTML).
    """
    import httpx

    page_url = url.rstrip("/") or url
    headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    deadline = time.monotonic() + timeout_sec
    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers=headers,
    ) as client:
        streak = 0
        while time.monotonic() < deadline:
            try:
                r = await client.get(page_url)
                if _http_get_looks_healthy(r):
                    streak += 1
                    if streak >= 2:
                        if await _static_assets_ok(client, r.text or "", page_url):
                            return True
                        streak = 0
                        await asyncio.sleep(0.55)
                        continue
                    await asyncio.sleep(0.35)
                    continue
                streak = 0
                if r.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(0.6)
                    continue
            except Exception:
                streak = 0
            await asyncio.sleep(0.45)
    return False


async def start_next_preview_server(
    cwd: Path, port: int, *, use_dev: bool, detach: bool = False
) -> asyncio.subprocess.Process:
    """Run ``next start`` (production) or ``next dev`` on 0.0.0.0:port.

    Prefer production ``start`` after a successful ``next build`` — it matches what
    ``curl`` / browsers load and avoids dev-only transient 200→500 behaviour.

    If ``detach`` is true (POSIX), ``start_new_session`` is set so the preview
    process is less likely to die when the parent agent receives SIGINT (Ctrl+C).
    """
    script = "dev" if use_dev else "start"
    exec_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "stdout": asyncio.subprocess.DEVNULL,
        "stderr": asyncio.subprocess.DEVNULL,
        "env": _npm_env(Path(cwd)),
    }
    if detach:
        exec_kwargs["start_new_session"] = True
    npm_exe = resolve_npm_executable()
    proc = await asyncio.create_subprocess_exec(
        npm_exe,
        "run",
        script,
        "--",
        "--hostname",
        "0.0.0.0",
        "--port",
        str(port),
        **exec_kwargs,
    )
    return proc


# Backwards-compatible name for callers
async def start_next_dev(cwd: Path, port: int) -> asyncio.subprocess.Process:
    """Deprecated: use ``start_next_preview_server``."""
    return await start_next_preview_server(cwd, port, use_dev=True)


async def terminate_process(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
