"""Map tiles, fetched once and kept.

The site draws a route over a map. That is a privacy trade, and it is worth
stating rather than burying:

- The **pod** fetches tiles, never the browser. The tile server sees one cluster
  address, and sees tile coordinates rather than a track.
- Every tile is cached in Postgres permanently, so a route re-read a hundred
  times fetches nothing and says nothing further. The signal a tile server can
  build is one request per 256×256 square of the world, ever.
- Nothing identifying is sent. No referer, no cookies, and a User-Agent that
  names this as a personal tool, which OpenStreetMap's tile policy requires.

If that trade is unwanted, the tiles simply do not load: the route polyline is
drawn independently and remains readable on its own.
"""

from __future__ import annotations

import math
import urllib.error
import urllib.request

from .store import Store

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
# OSM's policy requires a User-Agent identifying the application, and refuses
# generic library defaults.
USER_AGENT = "apple-health/1.0 (personal training log; self-hosted)"
TILE_SIZE = 256
MAX_ZOOM = 16
TIMEOUT = 10.0


def lonlat_to_tile(lat: float, lon: float, z: int) -> tuple[float, float]:
    """Web Mercator tile coordinates, fractional.

    Fractional on purpose: the caller needs sub-tile precision to place a
    polyline over the tiles, and rounding here would misalign the track from
    the roads beneath it by up to a tile.
    """
    n = 2.0 ** z
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(max(min(lat, 85.05112878), -85.05112878))
    y = (1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n
    return x, y


def choose_zoom(bounds: dict, tiles_across: int = 4) -> int:
    """The largest zoom whose tiles still cover the route in `tiles_across`.

    Largest, not smallest: more zoom is more detail, and the constraint is only
    that the whole route still fits in a handful of tiles — a long ride at
    street level would be hundreds of them, which is neither fast nor polite.
    """
    for z in range(MAX_ZOOM, 0, -1):
        x0, y0 = lonlat_to_tile(bounds["max_lat"], bounds["min_lon"], z)
        x1, y1 = lonlat_to_tile(bounds["min_lat"], bounds["max_lon"], z)
        if (x1 - x0) <= tiles_across and (y1 - y0) <= tiles_across:
            return z
    return 1


def fetch(store: Store, z: int, x: int, y: int) -> tuple[bytes, str] | None:
    """One tile, from the cache or from upstream. None if it cannot be had.

    A failure is never fatal and never retried in a loop: the page draws its
    polyline regardless, and a missing tile leaves a blank square rather than an
    error. Retrying a tile server that is refusing us would be the rudest
    possible response to being refused.
    """
    with store.cursor() as cur:
        cur.execute("SELECT data, content_type FROM map_tiles"
                    " WHERE z=%s AND x=%s AND y=%s", (z, x, y))
        row = cur.fetchone()
        if row:
            return bytes(row["data"]), row["content_type"]

    if not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        return None

    req = urllib.request.Request(
        TILE_URL.format(z=z, x=x, y=y),
        headers={"User-Agent": USER_AGENT, "Accept": "image/png"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = resp.read()
            content_type = resp.headers.get("Content-Type", "image/png")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    if not data:
        return None

    with store.cursor() as cur:
        cur.execute(
            """INSERT INTO map_tiles (z, x, y, content_type, data)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (z, x, y) DO NOTHING""",
            (z, x, y, content_type, data))
    store.commit()
    return data, content_type


def layout(bounds: dict) -> dict:
    """Which tiles cover this route, and where the route sits on them.

    Returns the tile grid plus the pixel origin, so the caller can place both
    the images and the track in one coordinate space. Getting these from two
    different projections is how a track ends up in a field beside the road.
    """
    z = choose_zoom(bounds)
    x0, y0 = lonlat_to_tile(bounds["max_lat"], bounds["min_lon"], z)
    x1, y1 = lonlat_to_tile(bounds["min_lat"], bounds["max_lon"], z)
    tx0, ty0 = math.floor(min(x0, x1)), math.floor(min(y0, y1))
    tx1, ty1 = math.floor(max(x0, x1)), math.floor(max(y0, y1))
    return {
        "z": z,
        "x0": tx0, "y0": ty0,
        "cols": tx1 - tx0 + 1, "rows": ty1 - ty0 + 1,
        "width": (tx1 - tx0 + 1) * TILE_SIZE,
        "height": (ty1 - ty0 + 1) * TILE_SIZE,
    }


def project(points: list, grid: dict) -> list[tuple[float, float]]:
    """Route points into the tile grid's pixel space."""
    out = []
    for lat, lon in points:
        x, y = lonlat_to_tile(lat, lon, grid["z"])
        out.append(((x - grid["x0"]) * TILE_SIZE, (y - grid["y0"]) * TILE_SIZE))
    return out
