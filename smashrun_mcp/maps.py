"""Kartenbild und Höhenprofil eines Smashrun-Laufs.

- render_map(): zeichnet die GPS-Spur auf OSM-Kacheln (PNG), mit Start/Ziel, km-Marken und
  optional den Antritten aus analysis.py.
- elevation_profile(): Höhen aus dem Geländemodell von Open-Meteo (keine Schlüssel nötig),
  weil die Höhe der Uhr unzuverlässig ist. Liefert Anstieg/Abstieg, Extreme und Höhe je km.

Kacheln und Höhen werden in /data zwischengespeichert (OSM-Nutzungsrichtlinie: eigener
User-Agent, kein Massenabruf, Cache). Pillow wird nur für das Bild gebraucht.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger("smashrun.maps")

TILE_URL = os.environ.get("SMASHRUN_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
TILE_DIR = Path(os.environ.get("SMASHRUN_TILE_CACHE", "/data/tiles"))
ELEV_CACHE_FILE = Path(os.environ.get("SMASHRUN_ELEV_CACHE", "/data/elevation_cache.json"))
ELEVATION_URL = os.environ.get("SMASHRUN_ELEVATION_URL", "https://api.open-meteo.com/v1/elevation")
USER_AGENT = "smashrun-mcp/0.3 (private running coach; contact via Home Assistant owner)"
TILE = 256
MAX_TILES = 25  # Obergrenze je Bild, schont den Kachelserver
TILE_DEADLINE_S = 12.0
ELEV_STEP_M = 50.0
ELEV_BATCH = 100
ELEV_TTL_S = 365 * 24 * 3600
ATTRIBUTION = "© OpenStreetMap-Mitwirkende"


# --------------------------------------------------------------------------
# Kachel-Geometrie (Web-Mercator)
# --------------------------------------------------------------------------
def _world_px(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = TILE * (2 ** z)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(max(-85.0511, min(85.0511, lat))))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def _choose_zoom(pts: list[tuple[float, float, float]], max_w: int, max_h: int) -> int:
    """Größter Zoom (höchstens 17), bei dem Spur samt Rand ins Bild passt und höchstens
    MAX_TILES Kacheln gebraucht werden."""
    for z in range(17, 9, -1):
        xs, ys = zip(*(_world_px(la, lo, z) for la, lo, _ in pts))
        w, h = max(xs) - min(xs) + 80, max(ys) - min(ys) + 80
        if w <= max_w and h <= max_h:
            tiles = (math.floor(w / TILE) + 2) * (math.floor(h / TILE) + 2)
            if tiles <= MAX_TILES:
                return z
    return 10


# --------------------------------------------------------------------------
# Kacheln holen (mit Dateicache)
# --------------------------------------------------------------------------
def _tile_path(z: int, x: int, y: int) -> Path:
    return TILE_DIR / str(z) / str(x) / f"{y}.png"


def _fetch_tiles(tiles: list[tuple[int, int, int]]) -> dict[tuple[int, int, int], bytes]:
    out: dict[tuple[int, int, int], bytes] = {}
    missing = []
    for t in tiles:
        p = _tile_path(*t)
        try:
            out[t] = p.read_bytes()
        except OSError:
            missing.append(t)
    if not missing:
        return out
    deadline = time.monotonic() + TILE_DEADLINE_S
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(6.0, connect=4.0)) as client:
        for z, x, y in missing:
            if time.monotonic() > deadline:
                raise RuntimeError("Kartenkacheln: Zeitlimit erreicht.")
            try:
                resp = client.get(TILE_URL.format(z=z, x=x, y=y))
            except httpx.HTTPError as e:
                raise RuntimeError(f"Kartenkacheln nicht erreichbar ({type(e).__name__}).")
            if resp.status_code != 200:
                raise RuntimeError(f"Kartenkachel {z}/{x}/{y}: HTTP {resp.status_code}")
            out[(z, x, y)] = resp.content
            try:
                p = _tile_path(z, x, y)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(resp.content)
            except OSError as e:  # Cache ist optional
                log.warning("Kachel nicht cachebar: %s", e)
            time.sleep(0.1)  # höflich bleiben
    return out


# --------------------------------------------------------------------------
# Kartenbild
# --------------------------------------------------------------------------
def render_map(
    pts: list[tuple[float, float, float]],
    efforts: Optional[list[dict]] = None,
    size: int = 800,
) -> tuple[bytes, dict[str, Any]]:
    """PNG-Bytes plus Metadaten. efforts: Antritte aus analysis.analyze (km, len_m)."""
    from PIL import Image, ImageDraw, ImageFont  # Pillow erst hier, damit der Rest ohne läuft

    size = max(400, min(int(size), 1200))
    z = _choose_zoom(pts, size, size)
    world = [_world_px(la, lo, z) for la, lo, _ in pts]
    xs, ys = [p[0] for p in world], [p[1] for p in world]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    left, top = cx - size / 2, cy - size / 2
    tx0, tx1 = math.floor(left / TILE), math.floor((left + size) / TILE)
    ty0, ty1 = math.floor(top / TILE), math.floor((top + size) / TILE)
    wanted = [(z, x, y) for x in range(tx0, tx1 + 1) for y in range(ty0, ty1 + 1)]
    if len(wanted) > MAX_TILES:
        raise RuntimeError(f"Kartenausschnitt braucht {len(wanted)} Kacheln (Maximum {MAX_TILES}).")
    data = _fetch_tiles(wanted)

    img = Image.new("RGB", (size, size), (235, 235, 235))
    for (tz, tx, ty), blob in data.items():
        tile = Image.open(io.BytesIO(blob)).convert("RGB")
        img.paste(tile, (round(tx * TILE - left), round(ty * TILE - top)))
    draw = ImageDraw.Draw(img, "RGBA")
    xy = [(x - left, y - top) for x, y in world]

    draw.line(xy, fill=(255, 255, 255, 230), width=9, joint="curve")  # heller Rand
    draw.line(xy, fill=(0, 90, 220, 255), width=5, joint="curve")

    meta: dict[str, Any] = {"zoom": z, "tiles": len(wanted), "size_px": size}
    total = pts[-1][2]

    def at_dist(d: float) -> int:
        lo, hi = 0, len(pts) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if pts[mid][2] < d:
                lo = mid + 1
            else:
                hi = mid
        return lo

    try:
        font = ImageFont.load_default(size=16)
        small = ImageFont.load_default(size=13)
    except TypeError:  # ältere Pillow ohne size
        font = small = ImageFont.load_default()

    drawn = []
    for n, e in enumerate(efforts or [], start=1):
        a = at_dist(float(e["km"]) * 1000)
        b = at_dist(float(e["km"]) * 1000 + float(e["len_m"]))
        seg = xy[a:b + 1]
        if len(seg) >= 2:
            draw.line(seg, fill=(230, 40, 40, 255), width=7, joint="curve")
            mx, my = seg[len(seg) // 2]
            draw.ellipse([mx - 11, my - 11, mx + 11, my + 11], fill=(230, 40, 40, 255), outline=(255, 255, 255, 255))
            draw.text((mx, my), str(n), fill=(255, 255, 255, 255), font=font, anchor="mm")
            drawn.append(n)
    meta["efforts_drawn"] = len(drawn)

    km = 1
    while km * 1000 < total:
        x, y = xy[at_dist(km * 1000)]
        draw.ellipse([x - 9, y - 9, x + 9, y + 9], fill=(255, 255, 255, 235), outline=(60, 60, 60, 255))
        draw.text((x, y), str(km), fill=(30, 30, 30, 255), font=small, anchor="mm")
        km += 1

    # Ziel (rot) zuerst, Start (grün, kleiner) obenauf: bei Rundstrecken liegen beide übereinander.
    x, y = xy[-1]
    draw.ellipse([x - 11, y - 11, x + 11, y + 11], fill=(200, 30, 30, 255), outline=(255, 255, 255, 255), width=2)
    x, y = xy[0]
    draw.ellipse([x - 6, y - 6, x + 6, y + 6], fill=(30, 160, 60, 255), outline=(255, 255, 255, 255), width=2)

    label = f"{ATTRIBUTION}  ·  {total / 1000:.2f} km"
    tw = draw.textlength(label, font=small)
    draw.rectangle([0, size - 20, tw + 12, size], fill=(255, 255, 255, 200))
    draw.text((6, size - 17), label, fill=(40, 40, 40, 255), font=small)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), meta


# --------------------------------------------------------------------------
# Höhenprofil (Open-Meteo)
# --------------------------------------------------------------------------
def _elev_cache_load() -> dict[str, Any]:
    try:
        return json.loads(ELEV_CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _elev_cache_save(data: dict[str, Any]) -> None:
    try:
        if ELEV_CACHE_FILE.parent.is_dir():
            now = time.time()
            data = {k: v for k, v in data.items() if now - v.get("t", 0) < ELEV_TTL_S}
            ELEV_CACHE_FILE.write_text(json.dumps(data))
    except OSError as e:
        log.warning("Höhen-Cache nicht schreibbar: %s", e)


def sample_points(pts: list[tuple[float, float, float]], step_m: float = ELEV_STEP_M) -> list[tuple[float, float, float]]:
    out, next_d = [], 0.0
    for p in pts:
        if p[2] >= next_d:
            out.append(p)
            next_d = p[2] + step_m
    if out[-1] is not pts[-1]:
        out.append(pts[-1])
    return out


def fetch_elevations(coords: list[tuple[float, float]]) -> tuple[list[float], bool]:
    """Höhen (m ü. NN) je Koordinate. Rückgabe: (Höhen, aus_dem_cache)."""
    key = "e" + hashlib.sha1(
        json.dumps([[round(la, 5), round(lo, 5)] for la, lo in coords], separators=(",", ":")).encode()
    ).hexdigest()[:15]
    cache = _elev_cache_load()
    hit = cache.get(key)
    if hit and time.time() - hit.get("t", 0) < ELEV_TTL_S:
        return hit["h"], True
    heights: list[float] = []
    for i in range(0, len(coords), ELEV_BATCH):
        chunk = coords[i:i + ELEV_BATCH]
        params = {
            "latitude": ",".join(f"{la:.5f}" for la, _ in chunk),
            "longitude": ",".join(f"{lo:.5f}" for _, lo in chunk),
        }
        try:
            resp = httpx.get(ELEVATION_URL, params=params, headers={"User-Agent": USER_AGENT},
                             timeout=httpx.Timeout(8.0, connect=4.0))
        except httpx.HTTPError as e:
            raise RuntimeError(f"Open-Meteo nicht erreichbar ({type(e).__name__}).")
        if resp.status_code != 200:
            raise RuntimeError(f"Open-Meteo: HTTP {resp.status_code} {resp.text[:100]}".strip())
        try:
            elev = resp.json().get("elevation")
        except ValueError:
            raise RuntimeError("Open-Meteo: Antwort ist kein JSON.")
        if not isinstance(elev, list) or len(elev) != len(chunk):
            raise RuntimeError("Open-Meteo: unerwartete Antwort.")
        heights += [float(h) for h in elev]
    cache[key] = {"t": time.time(), "h": heights}
    _elev_cache_save(cache)
    return heights, False


def _smooth(h: list[float], k: int = 2) -> list[float]:
    """Gleitender Mittelwert (Fenster 2k+1), glättet das grobe Geländemodell (ca. 90 m Raster)."""
    out = []
    for i in range(len(h)):
        a, b = max(0, i - k), min(len(h), i + k + 1)
        out.append(sum(h[a:b]) / (b - a))
    return out


def elevation_profile(pts: list[tuple[float, float, float]], with_chart: bool = False) -> dict[str, Any]:
    sp = sample_points(pts)
    heights, cached = fetch_elevations([(p[0], p[1]) for p in sp])
    dist = [p[2] for p in sp]
    sm = _smooth(heights)
    up = down = 0.0
    thr = 1.0  # Schwelle gegen Rauschen (m)
    ref = sm[0]
    for h in sm[1:]:
        if h - ref >= thr:
            up += h - ref
            ref = h
        elif ref - h >= thr:
            down += ref - h
            ref = h
    per_km = []
    total = dist[-1]
    for k in range(0, int(total // 1000) + 1):
        d = k * 1000
        i = min(range(len(dist)), key=lambda j: abs(dist[j] - d))
        per_km.append({"km": k, "elevation_m": round(sm[i])})
    lo_i, hi_i = sm.index(min(sm)), sm.index(max(sm))
    res: dict[str, Any] = {
        "ascent_m": round(up),
        "descent_m": round(down),
        "min_m": round(min(sm)),
        "min_at_km": round(dist[lo_i] / 1000, 2),
        "max_m": round(max(sm)),
        "max_at_km": round(dist[hi_i] / 1000, 2),
        "start_m": round(sm[0]),
        "end_m": round(sm[-1]),
        "per_km": per_km,
        "points": len(sp),
        "from_cache": cached,
        "source": "Geländemodell über Open-Meteo (Copernicus DEM, Raster ca. 90 m, daher grob), geglättet",
    }
    if with_chart:
        res["_chart"] = _chart_png(dist, sm)
    return res


def _chart_png(dist: list[float], h: list[float]) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    W, H, pad_l, pad_b, pad_t, pad_r = 800, 300, 56, 34, 16, 16
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=13)
    except TypeError:
        font = ImageFont.load_default()
    lo, hi = min(h), max(h)
    span = max(hi - lo, 10.0)  # mindestens 10 m, damit ein flaches Profil nicht aufgebläht wird
    mid = (hi + lo) / 2
    lo, hi = mid - span / 2 - 2, mid + span / 2 + 2
    total = dist[-1]
    px = lambda x: pad_l + (W - pad_l - pad_r) * x / total
    py = lambda y: pad_t + (H - pad_t - pad_b) * (1 - (y - lo) / (hi - lo))
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        d.line([(pad_l, py(v)), (W - pad_r, py(v))], fill=(225, 225, 225))
        d.text((6, py(v) - 7), f"{v:.0f} m", fill=(80, 80, 80), font=font)
    for k in range(0, int(total // 1000) + 1):
        d.line([(px(k * 1000), H - pad_b), (px(k * 1000), H - pad_b + 4)], fill=(80, 80, 80))
        d.text((px(k * 1000) - 4, H - pad_b + 8), str(k), fill=(80, 80, 80), font=font)
    poly = [(px(x), py(y)) for x, y in zip(dist, h)]
    d.polygon(poly + [(px(total), H - pad_b), (px(0), H - pad_b)], fill=(190, 215, 245))
    d.line(poly, fill=(0, 90, 220), width=3)
    d.text((W - 150, 2), "Höhe über km", fill=(80, 80, 80), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
