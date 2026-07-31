"""One-time Box OAuth bootstrap for the Mac-side pipeline (ADR-003).

Creates the token store that ``apple_health.box_client.BoxClient`` uses.
Prerequisite: a Box Platform "Custom App" (OAuth 2.0 user authentication)
with redirect URI ``http://localhost:53682/callback`` and the
"Read and write all files and folders" scope.

Usage::

    uv run python tools/box_auth.py --client-id <id> --client-secret <secret>

Opens no browser itself — it prints the authorize URL, you open it, approve,
and the local callback server catches the redirect and exchanges the code.
Re-running replaces the existing token store (fresh grant).
"""

from __future__ import annotations

import argparse
import http.server
import secrets
import sys
import threading
import urllib.parse
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from apple_health.box_client import DEFAULT_TOKEN_STORE, TOKEN_URL, _write_private  # noqa: E402

PORT = 53682
REDIRECT = f"http://localhost:{PORT}/callback"
AUTH_URL = "https://account.box.com/api/oauth2/authorize"


class _Catcher(http.server.BaseHTTPRequestHandler):
    code: str | None = None
    state: str = ""

    def do_GET(self):  # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        ok = q.get("state", [""])[0] == _Catcher.state and "code" in q
        if ok:
            _Catcher.code = q["code"][0]
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"HealthSync: authorised, you can close this tab."
                         if ok else b"Bad state/code.")

    def log_message(self, *args):  # silence request logging
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bootstrap the Box token store.")
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", required=True)
    ap.add_argument("--store", type=Path, default=DEFAULT_TOKEN_STORE)
    args = ap.parse_args(argv)

    _Catcher.state = secrets.token_urlsafe(16)
    server = http.server.HTTPServer(("127.0.0.1", PORT), _Catcher)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    params = urllib.parse.urlencode({
        "client_id": args.client_id, "response_type": "code",
        "redirect_uri": REDIRECT, "state": _Catcher.state})
    print("Open this URL, log in to Box and approve:\n")
    print(f"  {AUTH_URL}?{params}\n")
    print("Waiting for the redirect on localhost…")

    while _Catcher.code is None:
        threading.Event().wait(0.2)
    server.shutdown()

    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code", "code": _Catcher.code,
        "client_id": args.client_id, "client_secret": args.client_secret,
        "redirect_uri": REDIRECT}, timeout=30)
    resp.raise_for_status()
    tok = resp.json()
    _write_private(args.store, {
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "refresh_token": tok["refresh_token"],
    })
    print(f"Token store written: {args.store}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
