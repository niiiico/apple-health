"""Push an OCI image tarball to a plain-HTTP registry.

`docker push` refuses an HTTP registry unless the daemon is configured to trust
it, which would mean editing the user's Docker settings and restarting it. The
registry's own API has no such objection, and `docker save` already writes OCI
layout — blobs addressed by digest — so the upload is a handful of HTTP calls.

Usage: uv run --with httpx python scripts/push_image.py <tarball> <registry> <repo> <tag>
"""

from __future__ import annotations

import json
import sys
import tarfile
import tempfile
from pathlib import Path

import httpx

MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)


def blob_path(root: Path, digest: str) -> Path:
    """Locate a blob in the OCI layout by its digest."""
    return root / "blobs" / digest.split(":")[0] / digest.split(":")[1]


def push_blob(client: httpx.Client, base: str, repo: str, digest: str, path: Path) -> str:
    """Upload one blob unless the registry already has it."""
    head = client.head(f"{base}/v2/{repo}/blobs/{digest}")
    if head.status_code == 200:
        return "exists"

    start = client.post(f"{base}/v2/{repo}/blobs/uploads/")
    start.raise_for_status()
    location = start.headers["Location"]
    if location.startswith("/"):
        location = base + location

    joiner = "&" if "?" in location else "?"
    with path.open("rb") as handle:
        put = client.put(
            f"{location}{joiner}digest={digest}",
            content=handle.read(),
            headers={"Content-Type": "application/octet-stream"},
        )
    put.raise_for_status()
    return "pushed"


def main() -> int:
    """Extract the tarball and push every blob, then the manifest."""
    tarball, registry, repo, tag = sys.argv[1:5]
    base = f"http://{registry}"

    with tempfile.TemporaryDirectory() as work:
        root = Path(work)
        with tarfile.open(tarball) as archive:
            archive.extractall(root, filter="data")

        index = json.loads((root / "index.json").read_text())
        entries = [m for m in index["manifests"] if m["mediaType"] in MANIFEST_TYPES]
        if not entries:
            # A multi-platform save nests an index; follow it to the image.
            nested = json.loads(blob_path(root, index["manifests"][0]["digest"]).read_text())
            entries = [m for m in nested["manifests"] if m["mediaType"] in MANIFEST_TYPES]
        entry = entries[0]

        manifest_bytes = blob_path(root, entry["digest"]).read_bytes()
        manifest = json.loads(manifest_bytes)

        with httpx.Client(timeout=300.0) as client:
            blobs = [manifest["config"]] + manifest["layers"]
            for number, blob in enumerate(blobs, start=1):
                state = push_blob(client, base, repo, blob["digest"], blob_path(root, blob["digest"]))
                size = blob["size"] // 1024
                print(f"  [{number}/{len(blobs)}] {blob['digest'][7:19]}  {size:>7} KiB  {state}")

            put = client.put(
                f"{base}/v2/{repo}/manifests/{tag}",
                content=manifest_bytes,
                headers={"Content-Type": manifest["mediaType"]},
            )
            put.raise_for_status()
            print(f"manifest -> {repo}:{tag}  digest {put.headers.get('Docker-Content-Digest', '?')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
