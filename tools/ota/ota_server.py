#!/usr/bin/env python3
"""Serve the newest ad-hoc .ipa for local over-the-air install on an iPhone.

iOS only accepts an itms-services manifest over HTTPS, so this serves it with
the self-signed cert in `serve/`. The iPhone must have `serve/ota-ca.crt`
installed *and* fully trusted (Settings -> General -> About -> Certificate
Trust Settings) before the install link will work.

    uv run python tools/ota/ota_server.py

Then open https://<mac-ip>:8443/ in Safari on the iPhone. The .ipa is picked
from `distrib/` by mtime on every request, so rebuilding in Xcode is enough --
no restart, no copying. `/udid` reports the phone's own UDID and checks it
against the build's ad-hoc provisioning profile.
"""

from __future__ import annotations

import argparse
import html
import http.server
import os
import plistlib
import re
import ssl
import threading
import urllib.parse
import uuid
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE / "serve"
DISTRIB = HERE.parent.parent / "distrib"

# installd is picky: the manifest must be XML and the payload a binary blob.
MIME = {
    ".ipa": "application/octet-stream",
    ".plist": "application/xml",
    ".crt": "application/x-x509-ca-cert",
    ".mobileconfig": "application/x-apple-aspen-config",
}

# Stable across a run so reinstalling the profile replaces rather than stacks.
ENROLL_UUID = str(uuid.uuid4()).upper()


class NoBuild(Exception):
    """No .ipa found under distrib/."""


def newest_ipa() -> Path:
    ipas = sorted(DISTRIB.rglob("*.ipa"), key=lambda p: p.stat().st_mtime)
    if not ipas:
        raise NoBuild(f"no .ipa under {DISTRIB}")
    return ipas[-1]


def _app_file(ipa: Path, name: str) -> bytes:
    """Read Payload/<app>.app/<name> out of the .ipa."""
    with zipfile.ZipFile(ipa) as z:
        for n in z.namelist():
            if re.fullmatch(rf"Payload/[^/]+\.app/{re.escape(name)}", n):
                return z.read(n)
    raise NoBuild(f"{name} missing from {ipa.name}")


def build_info(ipa: Path) -> dict:
    """Bundle metadata + the UDIDs the embedded profile is provisioned for."""
    info = plistlib.loads(_app_file(ipa, "Info.plist"))
    # The .mobileprovision is CMS-signed; the payload plist sits inside it.
    blob = _app_file(ipa, "embedded.mobileprovision")
    m = re.search(rb"<\?xml.*?</plist>", blob, re.S)
    prof = plistlib.loads(m.group(0)) if m else {}
    return {
        "path": ipa,
        "bundle_id": info.get("CFBundleIdentifier", ""),
        "version": info.get("CFBundleShortVersionString", ""),
        "build": info.get("CFBundleVersion", ""),
        "title": info.get("CFBundleDisplayName") or info.get("CFBundleName", "App"),
        "min_os": info.get("MinimumOSVersion", ""),
        "profile": prof.get("Name", ""),
        "expires": prof.get("ExpirationDate", ""),
        "udids": {u.lower() for u in prof.get("ProvisionedDevices", [])},
    }


def manifest(base: str, b: dict) -> bytes:
    asset = lambda kind, url: {"kind": kind, "url": f"{base}/{url}"}
    return plistlib.dumps(
        {
            "items": [
                {
                    "assets": [
                        asset("software-package", "App.ipa"),
                        asset("display-image", "icon-57.png"),
                        asset("full-size-image", "icon-512.png"),
                    ],
                    "metadata": {
                        "bundle-identifier": b["bundle_id"],
                        "bundle-version": b["version"],
                        "kind": "software",
                        "title": b["title"],
                    },
                }
            ]
        }
    )


def enroll_profile(base: str) -> bytes:
    """Profile-service payload: iOS POSTs the device's UDID back to `base`/udid.

    Unsigned, so iOS flags it "Not Verified" on install -- fine for a profile
    that only ever talks to your own Mac, and it is removed right after.
    """
    return plistlib.dumps(
        {
            "PayloadType": "Profile Service",
            "PayloadIdentifier": "net.dev2.healthsync.ota.enroll",
            "PayloadUUID": ENROLL_UUID,
            "PayloadVersion": 1,
            "PayloadDisplayName": "HealthSync — show this device's UDID",
            "PayloadDescription": "Reports this device's UDID to your Mac so the "
            "ad-hoc build can be checked. Remove it afterwards.",
            "PayloadOrganization": "Local OTA",
            "PayloadContent": {
                "URL": f"{base}/udid",
                "DeviceAttributes": ["UDID", "PRODUCT", "VERSION", "SERIAL"],
            },
        }
    )


PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
:root {{ color-scheme: light dark; }}
body {{ font: 17px/1.5 -apple-system, system-ui, sans-serif; margin: 0; padding: 24px; max-width: 34rem; }}
h1 {{ font-size: 1.5rem; margin: 0 0 .25rem; }}
p.sub {{ color: #888; margin: 0 0 2rem; }}
ol {{ padding-left: 1.2rem; }} li {{ margin-bottom: 1.4rem; }}
a.btn {{ display: block; text-align: center; text-decoration: none; background: #e03b54; color: #fff;
        padding: 14px 18px; border-radius: 12px; font-weight: 600; margin-top: .6rem; }}
a.btn.alt {{ background: #4a5568; }}
code {{ background: rgba(128,128,128,.18); padding: 1px 5px; border-radius: 4px; font-size: .9em;
        word-break: break-all; }}
small {{ color: #888; }}
.ok {{ color: #12805c; font-weight: 600; }} .bad {{ color: #c11; font-weight: 600; }}
</style></head><body>{body}</body></html>
"""


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static `serve/` handler plus generated manifest, .ipa and UDID routes."""

    protocol_version = "HTTP/1.1"
    server_version = "OTA/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def guess_type(self, path):
        return MIME.get(Path(path).suffix.lower()) or super().guess_type(path)

    @property
    def _hostname(self) -> str:
        """Host header without its port, so any SAN in the cert works."""
        host = self.headers.get("Host", "")
        return host.rsplit(":", 1)[0] if ":" in host else host

    @property
    def base(self) -> str:
        """Origin for everything iOS requires over TLS: manifest, .ipa, profile."""
        return f"https://{self._hostname}:{self.server.https_port}"

    @property
    def http_base(self) -> str:
        """Plain-HTTP origin, used only to hand out the CA before it is trusted."""
        return f"http://{self._hostname}:{self.server.http_port}"

    def log_message(self, fmt, *args):
        """Log the User-Agent too: it distinguishes Safari from itunesstored,
        which is the difference between "page loaded" and "install worked"."""
        ua = self.headers.get("User-Agent", "-") if self.headers else "-"
        super().log_message("%s  [%s]", fmt % args, ua[:60])

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        route = urllib.parse.urlparse(self.path).path
        try:
            if route == "/":
                return self._html(self._index())
            if route == "/manifest.plist":
                return self._bytes(manifest(self.base, build_info(newest_ipa())),
                                   "application/xml")
            if route == "/App.ipa":
                return self._file(newest_ipa())
            if route == "/enroll.mobileconfig":
                return self._bytes(enroll_profile(self.base),
                                   "application/x-apple-aspen-config")
            if route == "/udid":
                return self._html(self._udid_result())
        except NoBuild as e:
            return self._html(PAGE.format(title="No build",
                                          body=f"<h1>No build found</h1><p>{html.escape(str(e))}</p>"),
                              status=503)
        return super().do_GET()

    def do_POST(self):
        """iOS posts a CMS-signed plist of device attributes to /udid."""
        if urllib.parse.urlparse(self.path).path != "/udid":
            return self.send_error(404)
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        m = re.search(rb"<\?xml.*?</plist>", body, re.S)
        attrs = plistlib.loads(m.group(0)) if m else {}
        # Bounce back into Safari with the result; the device has no browser here.
        q = urllib.parse.urlencode({k: attrs.get(k, "") for k in
                                    ("UDID", "PRODUCT", "VERSION", "SERIAL")})
        self.send_response(302)
        self.send_header("Location", f"{self.base}/udid?{q}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- page bodies -------------------------------------------------------

    def _index(self) -> str:
        b = build_info(newest_ipa())
        rel = b["path"].relative_to(DISTRIB.parent)
        install = (f"itms-services://?action=download-manifest&amp;"
                   f"url={self.base}/manifest.plist")
        # Steps 1-2 are not optional: dismissing Safari's certificate warning
        # only creates a Safari-local exception, and the daemon that downloads
        # the .ipa does not honour it -- it fails with "Unable to Install".
        body = f"""
<h1>{html.escape(b['title'])} {html.escape(b['version'])} (build {html.escape(b['build'])})</h1>
<p class="sub">{html.escape(b['bundle_id'])} · iOS {html.escape(str(b['min_os']))}+ ·
   {html.escape(str(len(b['udids'])))} provisioned devices<br>
   <small>{html.escape(str(rel))}</small></p>
<ol>
  <li><b>Install the certificate</b> (once).
    <a class="btn" href="{self.http_base}/ota-ca.crt">1 · Download certificate</a>
    <small>Then: Settings → Profile Downloaded → Install.</small></li>
  <li><b>Trust it fully</b> (once).<br>
    Settings → General → About → <b>Certificate Trust Settings</b> → enable
    <code>HealthSync Local OTA CA</code>.
    <small>Skipping this is what causes “Unable to Install”. Tapping through
    Safari’s warning is <b>not</b> a substitute — it only exempts Safari, not the
    installer.</small></li>
  <li><b>Install the app.</b>
    <a class="btn" href="{install}">2 · Install {html.escape(b['title'])}</a>
    <small>Safari only. Then check the Home Screen.</small></li>
</ol>
<hr><p><small>“Unable to Install” usually means this iPhone is not in the ad-hoc
profile. Check it:</small></p>
<a class="btn alt" href="/enroll.mobileconfig">Show this device’s UDID</a>
"""
        return PAGE.format(title=f"{b['title']} — local OTA", body=body)

    def _udid_result(self) -> str:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        got = (q.get("UDID") or [""])[0]
        if not got:
            return PAGE.format(title="UDID", body="<h1>No UDID</h1>"
                               "<p>Open <a href='/enroll.mobileconfig'>the profile</a> "
                               "on the iPhone and install it.</p>")
        b = build_info(newest_ipa())
        known = got.lower() in b["udids"]
        verdict = ("<p class='ok'>✓ This device is in the ad-hoc profile — the build "
                   "will install.</p>" if known else
                   "<p class='bad'>✗ This device is NOT in the ad-hoc profile.</p>"
                   "<p>Add the UDID at developer.apple.com → Devices, regenerate the "
                   "profile, then re-archive and re-export in Xcode.</p>")
        rows = "".join(
            f"<p><small>{k}</small><br><code>{html.escape((q.get(k) or [''])[0])}</code></p>"
            for k in ("UDID", "PRODUCT", "VERSION", "SERIAL") if q.get(k, [""])[0]
        )
        return PAGE.format(title="Device UDID", body=f"""
<h1>This device</h1>{rows}{verdict}
<p><small>Now remove the profile: Settings → General → VPN &amp; Device Management →
HealthSync — show this device’s UDID → Remove.</small></p>
<p><a class="btn" href="/">Back</a></p>""")

    # -- response helpers --------------------------------------------------

    def _bytes(self, data: bytes, ctype: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, text: str, status: int = 200):
        self._bytes(text.encode(), "text/html; charset=utf-8", status)

    def _file(self, path: Path):
        """Serve a file with Range support.

        installd fetches the .ipa with a Range request; a 200 with the whole
        body in reply to Range makes it fail.
        """
        size = path.stat().st_size
        ctype = self.guess_type(str(path))
        rng = self.headers.get("Range")
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip()) if rng else None

        if not m or not (m.group(1) or m.group(2)):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with path.open("rb") as f:
                self.copyfile(f, self.wfile)
            return

        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        else:  # suffix range: last N bytes
            start, end = max(0, size - int(m.group(2))), size - 1
        end = min(end, size - 1)

        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(206)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8443, help="HTTPS port")
    ap.add_argument("--http-port", type=int, default=8080,
                    help="plain HTTP port, for handing out the CA certificate")
    args = ap.parse_args()

    try:
        b = build_info(newest_ipa())
        print(f"newest build: {b['path'].relative_to(DISTRIB.parent)} "
              f"({b['title']} {b['version']}/{b['build']}, {len(b['udids'])} devices)")
    except NoBuild as e:
        print(f"warning: {e} — build & export from Xcode, then reload the page")

    def make(port: int) -> http.server.ThreadingHTTPServer:
        srv = http.server.ThreadingHTTPServer((args.host, port), Handler)
        srv.https_port, srv.http_port = args.port, args.http_port
        return srv

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # Full chain: the phone needs the CA presented, not just the leaf.
    ctx.load_cert_chain(ROOT / "ota-fullchain.pem", ROOT / "ota-key.pem")

    # The CA has to be reachable *before* it is trusted, so the landing page and
    # the .crt are also served over plain HTTP. iOS requires TLS for the
    # manifest and the .ipa, so those only ever work on the HTTPS port.
    plain = make(args.http_port)
    threading.Thread(target=plain.serve_forever, daemon=True).start()

    httpd = make(args.port)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print(f"serving on https://{args.host}:{args.port}/ "
          f"(cert bootstrap: http://{args.host}:{args.http_port}/)  (ctrl-c to stop)")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
