"""Minimal Box API client for the sync pipeline (ADR-003).

Auth: OAuth 2.0 user grant with rotating refresh tokens. Box refresh tokens
are single-use — every refresh returns a new one, which MUST be persisted
immediately or the chain is lost. Tokens live in a 0600 JSON file
(``~/.config/apple-health/box_tokens.json`` by default), written atomically.
Bootstrap the file once with ``tools/box_auth.py``.

Only the endpoints the pipeline needs are wrapped: folder find/create, folder
listing, file download, upload (with 409 → new-version fallback).

Usage::

    from apple_health.box_client import BoxClient
    box = BoxClient()                       # reads token store
    fid = box.ensure_folder("HealthSync")   # folder id at root
    for item in box.list_folder(fid): ...
    box.upload(fid, "delta-x.json", b"...")
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import requests

API = "https://api.box.com/2.0"
UPLOAD_API = "https://upload.box.com/api/2.0"
TOKEN_URL = "https://api.box.com/oauth2/token"
DEFAULT_TOKEN_STORE = Path("~/.config/apple-health/box_tokens.json").expanduser()


class BoxAuthError(RuntimeError):
    """Token store missing/invalid or the refresh chain is dead."""


@dataclass
class BoxItem:
    """One entry of a folder listing."""

    id: str
    name: str
    type: str  # "file" | "folder" | "web_link"


def _write_private(path: Path, data: dict) -> None:
    """Atomically write ``data`` as JSON with 0600 permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".box_tokens.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class BoxClient:
    """Box API wrapper with rotating-refresh-token auth.

    The access token is refreshed lazily on the first request and on any 401.
    Every refresh persists the *new* refresh token before returning (rotation
    — see ADR-003).
    """

    def __init__(self, token_store: Path = DEFAULT_TOKEN_STORE) -> None:
        self._store_path = token_store
        if not token_store.exists():
            raise BoxAuthError(
                f"No token store at {token_store} — run `uv run python tools/box_auth.py` once."
            )
        self._store = json.loads(token_store.read_text())
        for key in ("client_id", "client_secret", "refresh_token"):
            if key not in self._store:
                raise BoxAuthError(f"Token store missing {key!r}: {token_store}")
        self._access_token: str | None = None

    # -- auth ---------------------------------------------------------------

    def _refresh(self) -> None:
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": self._store["refresh_token"],
            "client_id": self._store["client_id"],
            "client_secret": self._store["client_secret"],
        }, timeout=30)
        if resp.status_code != 200:
            raise BoxAuthError(
                f"Refresh failed ({resp.status_code}): {resp.text[:200]} — "
                "if the chain is dead (>60 days idle), re-run tools/box_auth.py."
            )
        tok = resp.json()
        self._access_token = tok["access_token"]
        self._store["refresh_token"] = tok["refresh_token"]
        _write_private(self._store_path, self._store)

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Authenticated request; refreshes the token lazily and once on 401."""
        if self._access_token is None:
            self._refresh()
        for attempt in (0, 1):
            headers = kwargs.pop("headers", {}) | {
                "Authorization": f"Bearer {self._access_token}"}
            resp = requests.request(method, url, headers=headers, timeout=120, **kwargs)
            if resp.status_code == 401 and attempt == 0:
                self._refresh()
                continue
            return resp
        raise AssertionError("unreachable")

    # -- folders ------------------------------------------------------------

    def list_folder(self, folder_id: str) -> list[BoxItem]:
        """All items of a folder (paginates past 1000)."""
        items: list[BoxItem] = []
        offset = 0
        while True:
            resp = self._request(
                "GET", f"{API}/folders/{folder_id}/items",
                params={"limit": 1000, "offset": offset, "fields": "id,name,type"})
            resp.raise_for_status()
            body = resp.json()
            items += [BoxItem(e["id"], e["name"], e["type"]) for e in body["entries"]]
            offset += len(body["entries"])
            if offset >= body["total_count"] or not body["entries"]:
                return items

    def ensure_folder(self, name: str, parent_id: str = "0") -> str:
        """Return the id of ``parent/name``, creating the folder if absent."""
        for item in self.list_folder(parent_id):
            if item.type == "folder" and item.name == name:
                return item.id
        resp = self._request("POST", f"{API}/folders", json={
            "name": name, "parent": {"id": parent_id}})
        resp.raise_for_status()
        return resp.json()["id"]

    # -- files --------------------------------------------------------------

    def download(self, file_id: str) -> bytes:
        resp = self._request("GET", f"{API}/files/{file_id}/content")
        resp.raise_for_status()
        return resp.content

    def upload(self, folder_id: str, name: str, content: bytes) -> str:
        """Upload ``name`` into ``folder_id``; on name conflict, upload a new
        version of the existing file. Returns the file id."""
        resp = self._request(
            "POST", f"{UPLOAD_API}/files/content",
            data={"attributes": json.dumps({"name": name, "parent": {"id": folder_id}})},
            files={"file": (name, content)})
        if resp.status_code == 409:
            existing = resp.json().get("context_info", {}).get("conflicts", {}).get("id")
            if not existing:
                resp.raise_for_status()
            return self.upload_version(existing, name, content)
        resp.raise_for_status()
        return resp.json()["entries"][0]["id"]

    def upload_version(self, file_id: str, name: str, content: bytes) -> str:
        resp = self._request(
            "POST", f"{UPLOAD_API}/files/{file_id}/content",
            files={"file": (name, content)})
        resp.raise_for_status()
        return resp.json()["entries"][0]["id"]
