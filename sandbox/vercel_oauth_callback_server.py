"""Optional local server for ``VERCEL_OAUTH_REDIRECT_URI`` (manual / debugging).

When **begin_vercel_oauth** runs from the agent with a loopback redirect URI, the agent
listens automatically — you usually **do not** need this script.

Run::

    uv run python -m sandbox.vercel_oauth_callback_server

Requires ``VERCEL_OAUTH_REDIRECT_URI`` with an explicit host and port, e.g.
``http://127.0.0.1:3939/oauth/vercel/callback``.
"""

from __future__ import annotations

import html
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import dotenv

    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dotenv.load_dotenv(os.path.join(_root, ".env"))
except ImportError:
    pass

from sandbox.vercel_oauth import _html_oauth_page


def _page(title: str, body: str) -> bytes:
    return _html_oauth_page(title, body)


class _Handler(BaseHTTPRequestHandler):
    expected_path: str = "/"
    scheme: str = "http"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        if path != self.expected_path:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        qs = parse_qs(parsed.query)
        flat = {k: (v[0] if v else "") for k, v in qs.items()}
        host = self.headers.get("Host", "")
        display_url = f"{self.scheme}://{host}{self.path}"

        if flat.get("error"):
            err = html.escape(flat.get("error", ""))
            desc = html.escape((flat.get("error_description") or "").replace("+", " "))
            body = f'<h1 class="err">Vercel OAuth error</h1><p><strong>{err}</strong></p><p>{desc}</p>'
            self.send_response(200)
        elif flat.get("code"):
            esc_url = html.escape(display_url)
            body = f"""<h1 class="ok">Vercel redirect received</h1>
<p>Copy the URL below and paste it into the chat, then run <strong>complete_vercel_oauth</strong>.</p>
<textarea readonly id="u">{esc_url}</textarea>
<p><button type="button" onclick="navigator.clipboard.writeText(document.getElementById('u').value)">Copy URL</button></p>"""
            self.send_response(200)
        else:
            body = (
                "<h1>Waiting for OAuth redirect</h1>"
                "<p>Open the Vercel authorize link, approve, and you will land here with <code>code=</code>.</p>"
            )
            self.send_response(200)

        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(_page("Vercel OAuth callback", body))


def main() -> int:
    raw = (os.getenv("VERCEL_OAUTH_REDIRECT_URI") or "").strip()
    if not raw:
        print("Set VERCEL_OAUTH_REDIRECT_URI in .env", file=sys.stderr)
        return 1
    u = urlparse(raw)
    if u.scheme not in ("http", "https"):
        print("VERCEL_OAUTH_REDIRECT_URI must start with http:// or https://", file=sys.stderr)
        return 1
    if not u.hostname or u.port is None:
        print(
            "This helper needs an explicit port in VERCEL_OAUTH_REDIRECT_URI, e.g.\n"
            "  http://127.0.0.1:3939/oauth/vercel/callback",
            file=sys.stderr,
        )
        return 1

    path = u.path or "/"
    _Handler.expected_path = path
    _Handler.scheme = u.scheme

    bind_host = u.hostname
    port = u.port
    server = HTTPServer((bind_host, port), _Handler)
    print(f"Listening on http://{bind_host}:{port}{path}")
    print("Open the authorize link in the browser, approve, then copy the URL from this page (or stop with Ctrl+C).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
