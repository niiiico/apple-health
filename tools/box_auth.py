"""One-time Box OAuth bootstrap for the Mac-side pipeline (ADR-003).

Creates the token store that ``apple_health.box_client.BoxClient`` uses.
Prerequisite: a Box Platform "Custom App" (OAuth 2.0 user authentication)
with a localhost redirect URI and the "Read and write all files and folders"
scope.

Usage::

    uv run python tools/box_auth.py --client-id <id> --client-secret <secret>

Box compares the redirect URI byte-for-byte and answers ``redirect_uri_mismatch``
on any difference — a trailing slash, ``127.0.0.1`` vs ``localhost``, a missing
path. Pass ``--redirect`` with whatever the Developer Console has saved rather
than editing this file; the callback binds that URI's host and port.

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

DEFAULT_REDIRECT = "http://localhost:53682/callback"
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


def _read_code(pasted: str, state: str) -> str:
    """Pull the ``code`` out of a pasted redirect URL, checking ``state``.

    Accepts a bare code too, for the case where the browser shows only that.
    Returns "" if the input carries a query string whose state does not match
    the one we sent — a mismatch means this is not our grant.
    """
    parsed = urllib.parse.urlparse(pasted)
    if not parsed.scheme:                       # bare code, not a URL
        return pasted if "?" not in pasted and "&" not in pasted else ""
    q = urllib.parse.parse_qs(parsed.query)
    if q.get("state", [state])[0] != state:     # not our grant
        return ""
    return q.get("code", [""])[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bootstrap the Box token store.")
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", required=True)
    ap.add_argument("--store", type=Path, default=DEFAULT_TOKEN_STORE)
    ap.add_argument(
        "--redirect", default=DEFAULT_REDIRECT,
        help="Must match the app's OAuth 2.0 Redirect URI in the Box Developer "
             "Console byte-for-byte, or Box returns redirect_uri_mismatch. "
             f"The callback listens on its host/port. Default: {DEFAULT_REDIRECT}")
    args = ap.parse_args(argv)

    url = urllib.parse.urlparse(args.redirect)
    if url.scheme not in ("http", "https") or not url.hostname:
        ap.error(f"--redirect must be an absolute http(s) URL, got {args.redirect!r}")
    local = url.hostname in ("localhost", "127.0.0.1", "::1")

    state = secrets.token_urlsafe(16)
    _Catcher.state = state
    server = None
    if local:
        # Bind whatever the configured URI points at, rather than assuming a port.
        port = url.port or (443 if url.scheme == "https" else 80)
        server = http.server.HTTPServer((url.hostname, port), _Catcher)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    params = urllib.parse.urlencode({
        "client_id": args.client_id, "response_type": "code",
        "redirect_uri": args.redirect, "state": state})
    print("Open this URL, log in to Box and approve:\n")
    print(f"  {AUTH_URL}?{params}\n")

    if local:
        print("Waiting for the redirect on localhost…")
        while _Catcher.code is None:
            threading.Event().wait(0.2)
        code = _Catcher.code
        server.shutdown()
    else:
        # Box now requires https redirect URIs, so a localhost callback often
        # cannot be registered at all. With a remote URI nothing can catch the
        # redirect locally — the browser simply lands on that page with the
        # code in the query string, so read it back from the address bar.
        print(f"Box will send you to {url.scheme}://{url.netloc}{url.path} — that page\n"
              "may look unrelated or 404. That is fine: copy its full URL from the\n"
              "address bar (it carries ?code=… ) and paste it here.\n")
        pasted = input("Redirected URL (or bare code): ").strip()
        code = _read_code(pasted, state) if pasted else ""
        if not code:
            print("No authorization code found in that input.", file=sys.stderr)
            return 1

    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code", "code": code,
        "client_id": args.client_id, "client_secret": args.client_secret,
        "redirect_uri": args.redirect}, timeout=30)
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
