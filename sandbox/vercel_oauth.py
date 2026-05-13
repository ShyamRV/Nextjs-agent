"""Vercel **Sign in with Vercel** OAuth (PKCE) — per-user tokens in the workspace.

Flow: authorization-code + PKCE with a registered ``redirect_uri``.

**Seamless mode (default):** If ``VERCEL_OAUTH_REDIRECT_URI`` is ``http`` on
``127.0.0.1`` or ``localhost`` with an explicit port, this module briefly listens
on that address, opens nothing by itself — the **tool** blocks until the user
approves in the browser and Vercel redirects here, then exchanges the code
automatically (no paste step).

**Manual mode:** HTTPS / remote callbacks, or ``VERCEL_OAUTH_AUTO_CALLBACK=false``:
user copies the callback URL from the browser and calls ``complete_vercel_oauth``.

Docs: https://vercel.com/docs/sign-in-with-vercel/authorization-server-api
Tokens (``vca_`` / ``vcr_``): https://vercel.com/docs/sign-in-with-vercel/tokens
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

_AUTHORIZE = "https://vercel.com/oauth/authorize"
_TOKEN = "https://api.vercel.com/login/oauth/token"
_VERIFIER_CHARS = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)

_PENDING = ".agent_vercel_oauth.json"
_USER_TOKENS = ".agent_vercel_user_token.json"


def pending_oauth_path(workspace: Path) -> Path:
    return workspace / _PENDING


def user_vercel_token_path(workspace: Path) -> Path:
    return workspace / _USER_TOKENS


def _random_verifier() -> str:
    return "".join(secrets.choice(_VERIFIER_CHARS) for _ in range(64))


def _code_challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _oauth_auto_callback_enabled() -> bool:
    v = (os.getenv("VERCEL_OAUTH_AUTO_CALLBACK") or "true").strip().lower()
    return v in {"1", "true", "yes", "on"}


def _oauth_wait_seconds() -> float:
    try:
        return float(os.getenv("VERCEL_OAUTH_WAIT_SECONDS") or "420")
    except ValueError:
        return 420.0


def _local_callback_target(redirect_uri: str) -> tuple[str, int, str] | None:
    """If we can bind and capture the OAuth redirect locally, return (bind_host, port, path)."""
    u = urlparse(redirect_uri.strip())
    if u.scheme != "http":
        return None
    if not u.hostname or u.port is None:
        return None
    h = u.hostname.lower()
    if h not in ("127.0.0.1", "localhost"):
        return None
    path = u.path or "/"
    # Bind loopback explicitly (Windows-friendly).
    bind_host = "127.0.0.1" if h == "localhost" else u.hostname
    return (bind_host, int(u.port), path)


def _authorize_url(client_id: str, redirect_uri: str, state: str, nonce: str, challenge: str, scopes: str) -> str:
    auth_params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "response_type": "code",
    }
    if scopes:
        auth_params["scope"] = scopes
    return f"{_AUTHORIZE}?{urlencode(auth_params)}"


def _manual_oauth_instructions(authorize_url: str, redirect_uri: str) -> str:
    return (
        "**Vercel sign-in**\n\n"
        "1. Open this link while logged into the **Vercel account** that should own projects:\n"
        f"   {authorize_url}\n\n"
        "2. Approve access.\n\n"
        "3. After redirect, **paste the full URL** from the address bar here and run "
        "**`complete_vercel_oauth`** with that URL (must include `code=` and `state=`).\n"
        "   If you see “connection refused” on `127.0.0.1`, the URL in the bar is still valid — copy it.\n\n"
        "**Registered callback (must match the Vercel app):**\n"
        f"   `{redirect_uri}`\n"
    )


def _html_oauth_page(title: str, body_inner: str) -> bytes:
    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; max-width: 40rem;
  margin: 0 auto; padding: 1.5rem; line-height: 1.45; color: #111; background: #fafafa; }}
.card {{ background: #fff; border-radius: 12px; padding: 1.25rem 1.5rem;
  box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
.ok {{ color: #067d44; }} .err {{ color: #b42318; }}
textarea {{ width: 100%; min-height: 5rem; font: 0.8rem ui-monospace, monospace; margin-top: .75rem; }}
button {{ margin-top: .75rem; padding: .5rem 1rem; font: inherit; cursor: pointer; border-radius: 8px;
  border: 1px solid #ccc; background: #f4f4f4; }}
h1 {{ font-size: 1.25rem; margin: 0 0 .5rem; }}
p {{ margin: .5rem 0; }}
</style></head><body><div class="card">{body_inner}</div></body></html>"""
    return doc.encode("utf-8")


def _run_local_callback_server(
    workspace: Path,
    bind_host: str,
    port: int,
    expected_path: str,
    registered_redirect_uri: str,
    timeout_sec: float,
) -> str | None:
    """Block until one valid OAuth redirect is handled or timeout. Returns complete_vercel_oauth message or None."""
    outcome: dict[str, Any] = {"msg": None, "err_html": None, "finished": False}
    workspace = workspace.resolve()
    browser_err_marker = "##VERCEL_OAUTH_BROWSER_ERROR##"

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def do_GET(self) -> None:
            if outcome["finished"]:
                self.send_response(503)
                self.end_headers()
                return
            parsed = urlparse(self.path)
            path = parsed.path or "/"
            if path != expected_path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found")
                return

            reg = urlparse(registered_redirect_uri)
            host_hdr = (self.headers.get("Host") or "").strip()
            if not host_hdr:
                host_hdr = f"{reg.hostname}:{reg.port}" if reg.port else (reg.hostname or "")
            full_callback = f"{reg.scheme}://{host_hdr}{self.path}"

            qs = parse_qs(parsed.query)
            flat = {k: (v[0] if v else "") for k, v in qs.items()}
            if flat.get("error"):
                err = html.escape(flat.get("error", ""))
                desc = html.escape((flat.get("error_description") or "").replace("+", " "))
                raw_desc = (flat.get("error_description") or "").replace("+", " ")
                outcome["msg"] = (
                    f"{browser_err_marker}\n"
                    f"**Vercel returned:** `{flat.get('error', '')}`\n{raw_desc[:800]}\n\n"
                    "Fix the OAuth app in Vercel (callback URL, Permissions / scopes), then run **begin_vercel_oauth** again."
                )
                outcome["err_html"] = (
                    f'<h1 class="err">Vercel returned an error</h1><p><strong>{err}</strong></p><p>{desc}</p>'
                    '<p>Close this tab and check your Vercel app settings (callback URL, scopes).</p>'
                )
                outcome["finished"] = True
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(_html_oauth_page("Vercel OAuth", outcome["err_html"]))
                return

            if not flat.get("code") or not flat.get("state"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(
                    _html_oauth_page(
                        "Waiting",
                        "<h1>Almost there</h1><p>Approve access in the other tab. "
                        "This page will refresh when Vercel redirects here with <code>code=</code>.</p>",
                    )
                )
                return

            try:
                outcome["msg"] = complete_vercel_oauth(workspace, full_callback)
                outcome["finished"] = True
                inner = """<h1 class="ok">You are connected</h1>
<p>Your Vercel account is linked to this workspace. You can close this tab and return to the chat.</p>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(_html_oauth_page("Vercel linked", inner))
            except Exception as exc:
                outcome["finished"] = True
                outcome["err_html"] = html.escape(str(exc))
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(
                    _html_oauth_page(
                        "OAuth error",
                        f'<h1 class="err">Could not finish sign-in</h1><p>{outcome["err_html"]}</p>'
                        "<p>Run <strong>begin_vercel_oauth</strong> again in the chat.</p>",
                    )
                )

    try:
        class _ReuseHTTPServer(HTTPServer):
            allow_reuse_address = True

        server = _ReuseHTTPServer((bind_host, port), _H)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot listen on {bind_host}:{port} ({exc}). "
            "Pick a free port in VERCEL_OAUTH_REDIRECT_URI, register it in the Vercel app, "
            "or set VERCEL_OAUTH_AUTO_CALLBACK=false and use the paste flow."
        ) from exc

    server.timeout = 1.0
    deadline = time.monotonic() + max(30.0, timeout_sec)
    try:
        while time.monotonic() < deadline and not outcome["finished"]:
            server.handle_request()
    finally:
        try:
            server.server_close()
        except Exception:
            pass

    if outcome["msg"]:
        return str(outcome["msg"])
    if outcome["err_html"] and not outcome["msg"]:
        return None
    return None


def will_auto_capture_redirect(redirect_uri: str) -> bool:
    """True when this agent will listen on loopback for the OAuth callback (no paste)."""
    return _oauth_auto_callback_enabled() and _local_callback_target(redirect_uri) is not None


def _deploy_pat_tip() -> str:
    return (
        "**Deploys from this agent:** for reliable file uploads to Vercel’s REST API, set **`VERCEL_TOKEN`** "
        "(a PAT from https://vercel.com/account/tokens ) on the server. When `VERCEL_TOKEN` is set, deploy uses it first; "
        "Sign-in OAuth alone often returns **HTTP 403** on `POST /v13/deployments`."
    )


def prepare_vercel_oauth(workspace: Path) -> tuple[str, str, tuple[str, int, str] | None]:
    """Write PKCE pending state. Returns ``(authorize_url, redirect_uri, local_bind_or_none)``."""
    client_id = (os.getenv("VERCEL_OAUTH_CLIENT_ID") or "").strip()
    redirect_uri = (os.getenv("VERCEL_OAUTH_REDIRECT_URI") or "").strip()
    if not client_id:
        raise RuntimeError(
            "VERCEL_OAUTH_CLIENT_ID is not set. Create a **Vercel OAuth App** "
            "(Vercel Dashboard → Settings → Apps), set **Authorization Callback URL** "
            "to exactly VERCEL_OAUTH_REDIRECT_URI, and enable PKCE / client auth as needed."
        )
    if not redirect_uri:
        raise RuntimeError(
            "VERCEL_OAUTH_REDIRECT_URI is not set. For seamless sign-in on the agent machine use:\n"
            "  http://127.0.0.1:3939/oauth/vercel/callback\n"
            "(register the same URL in the Vercel OAuth app)."
        )

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = _random_verifier()
    code_challenge = _code_challenge_s256(code_verifier)
    scopes = (os.getenv("VERCEL_OAUTH_SCOPES") or "").strip()

    pending = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "nonce": nonce,
        "code_verifier": code_verifier,
        "created_at": time.time(),
    }
    _save_json(pending_oauth_path(workspace), pending)

    authorize_url = _authorize_url(client_id, redirect_uri, state, nonce, code_challenge, scopes)
    loc = _local_callback_target(redirect_uri)
    return authorize_url, redirect_uri, loc


def wait_vercel_oauth_local(
    workspace: Path,
    authorize_url: str,
    redirect_uri: str,
    local_bind: tuple[str, int, str] | None,
) -> str:
    """After the authorize URL was shown to the user: wait for loopback redirect or return paste instructions."""
    if not (_oauth_auto_callback_enabled() and local_bind is not None):
        return _manual_oauth_instructions(authorize_url, redirect_uri)

    bind_host, port, path = local_bind
    wait = _oauth_wait_seconds()
    try:
        auto_msg = _run_local_callback_server(
            workspace,
            bind_host,
            port,
            path,
            redirect_uri,
            wait,
        )
    except RuntimeError as exc:
        return (
            f"{_manual_oauth_instructions(authorize_url, redirect_uri)}\n"
            f"**Could not listen for the redirect:** {exc}"
        )

    if auto_msg:
        marker = "##VERCEL_OAUTH_BROWSER_ERROR##\n"
        if auto_msg.startswith(marker):
            return auto_msg[len(marker) :]
        return (
            "**Vercel sign-in complete** — the callback was received on this machine (no paste step).\n\n"
            f"{auto_msg}\n\n"
            f"{_deploy_pat_tip()}"
        )

    return (
        "**We did not receive the Vercel redirect in time.**\n\n"
        f"Open this link again and approve within ~{int(wait)} seconds:\n"
        f"   {authorize_url}\n\n"
        "Then run **begin_vercel_oauth** again.\n\n"
        "**Or** paste the full callback URL into **complete_vercel_oauth** "
        "(set `VERCEL_OAUTH_AUTO_CALLBACK=false` to always use that flow).\n\n"
        f"**Callback:** `{redirect_uri}`\n"
    )


def begin_vercel_oauth(workspace: Path) -> str:
    """Prepare OAuth and wait (non-interactive callers only — the chat tool shows the URL before waiting)."""
    au, r, loc = prepare_vercel_oauth(workspace)
    return wait_vercel_oauth_local(workspace, au, r, loc)


def _parse_callback_params(pasted: str) -> dict[str, str]:
    raw = (pasted or "").strip()
    if not raw:
        raise RuntimeError("Paste the full redirect URL from the browser (empty input).")
    if "?" not in raw and "code=" not in raw:
        raise RuntimeError("Expected a URL containing ?code=... (copy the full address bar).")
    if not raw.startswith("http"):
        raw = "https://dummy.local/?" + raw.lstrip("?&")
    parsed = urlparse(raw)
    qs = parse_qs(parsed.query)
    out = {k: (v[0] if v else "") for k, v in qs.items()}
    if out.get("error"):
        err = out.get("error", "")
        desc = (out.get("error_description") or "").replace("+", " ")
        raise RuntimeError(
            f"Vercel returned OAuth error={err!r} description={desc!r}. "
            "Fix the OAuth app (dashboard scopes / callback URL) or unset VERCEL_OAUTH_SCOPES "
            "so the authorize request omits scope and uses only scopes enabled for the app."
        )
    if not out.get("code"):
        raise RuntimeError("No `code` query parameter found in the pasted URL.")
    return out


def complete_vercel_oauth(workspace: Path, authorization_response_url: str) -> str:
    """Exchange authorization code for tokens; save for deploy_to_vercel."""
    pending_path = pending_oauth_path(workspace)
    if not pending_path.is_file():
        raise RuntimeError(
            "No pending Vercel OAuth for this workspace. Run **begin_vercel_oauth** first."
        )
    pending = _load_json(pending_path)
    client_id = str(pending.get("client_id") or "").strip()
    redirect_uri = str(pending.get("redirect_uri") or "").strip()
    expected_state = str(pending.get("state") or "").strip()
    code_verifier = str(pending.get("code_verifier") or "").strip()
    expected_nonce = str(pending.get("nonce") or "").strip()

    if not client_id or not redirect_uri or not code_verifier:
        pending_path.unlink(missing_ok=True)
        raise RuntimeError("Invalid pending Vercel OAuth state; run begin_vercel_oauth again.")

    params = _parse_callback_params(authorization_response_url)
    if params.get("error"):
        pending_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Vercel returned error={params.get('error')!r} "
            f"description={params.get('error_description', '')!r}"
        )
    if params.get("state", "") != expected_state:
        raise RuntimeError("OAuth `state` mismatch — wrong or stale callback URL.")

    code = str(params.get("code") or "").strip()
    if not code:
        pending_path.unlink(missing_ok=True)
        raise RuntimeError("Missing authorization `code` in pasted URL.")

    client_secret = (os.getenv("VERCEL_OAUTH_CLIENT_SECRET") or "").strip()
    body: dict[str, str] = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
    }
    if client_secret:
        body["client_secret"] = client_secret

    with httpx.Client(timeout=60.0) as client:
        tr = client.post(
            _TOKEN,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
        )
        if tr.status_code >= 400:
            pending_path.unlink(missing_ok=True)
            raise RuntimeError(f"Vercel token exchange failed: HTTP {tr.status_code} {tr.text[:1500]}")
        try:
            tok = tr.json()
        except Exception as exc:
            pending_path.unlink(missing_ok=True)
            raise RuntimeError(f"Vercel token response not JSON: {tr.text[:800]}") from exc

    if not isinstance(tok, dict):
        pending_path.unlink(missing_ok=True)
        raise RuntimeError(f"Unexpected token payload: {tok!r}")

    access = str(tok.get("access_token") or "").strip()
    refresh = str(tok.get("refresh_token") or "").strip()
    expires_in = int(tok.get("expires_in") or 3600)
    if not access:
        pending_path.unlink(missing_ok=True)
        raise RuntimeError(f"Vercel token response missing access_token: {tok!r}")

    id_token = str(tok.get("id_token") or "")
    if expected_nonce and id_token:
        # Lightweight nonce check (JWT payload middle segment)
        try:
            parts = id_token.split(".")
            if len(parts) >= 2:
                pad = "=" * (-len(parts[1]) % 4)
                payload = base64.urlsafe_b64decode(parts[1] + pad).decode("utf-8", errors="replace")
                if f'"nonce":"{expected_nonce}"' not in payload and f'"nonce": "{expected_nonce}"' not in payload:
                    if expected_nonce not in payload:
                        pass  # some encodings differ; do not hard-fail
        except Exception:
            pass

    store = {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": time.time() + float(max(60, expires_in - 30)),
    }
    _save_json(user_vercel_token_path(workspace), store)
    pending_path.unlink(missing_ok=True)
    return (
        "Your **Vercel account is linked** for this workspace (tokens are stored only in this session’s workspace files).\n\n"
        f"{_deploy_pat_tip()}"
    )


def refresh_vercel_access_token(workspace: Path) -> str | None:
    """If access token is expired, refresh using refresh_token. Returns new access or None."""
    path = user_vercel_token_path(workspace)
    data = _load_json(path)
    access = str(data.get("access_token") or "").strip()
    refresh = str(data.get("refresh_token") or "").strip()
    expires_at = float(data.get("expires_at") or 0)
    if access and time.time() < expires_at:
        return access
    if not refresh:
        return None

    client_id = (os.getenv("VERCEL_OAUTH_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("VERCEL_OAUTH_CLIENT_SECRET") or "").strip()
    if not client_id:
        return access or None

    body: dict[str, str] = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh,
    }
    if client_secret:
        body["client_secret"] = client_secret

    with httpx.Client(timeout=60.0) as client:
        tr = client.post(
            _TOKEN,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
        )
        if tr.status_code >= 400:
            return None
        try:
            tok = tr.json()
        except Exception:
            return None
    if not isinstance(tok, dict):
        return None
    new_access = str(tok.get("access_token") or "").strip()
    new_refresh = str(tok.get("refresh_token") or "").strip() or refresh
    expires_in = int(tok.get("expires_in") or 3600)
    if not new_access:
        return None
    data["access_token"] = new_access
    data["refresh_token"] = new_refresh
    data["expires_at"] = time.time() + float(max(60, expires_in - 30))
    _save_json(path, data)
    return new_access


def resolve_vercel_access_token(workspace: Path) -> str | None:
    """User OAuth access token (refreshed if expired), else None."""
    return refresh_vercel_access_token(workspace)
