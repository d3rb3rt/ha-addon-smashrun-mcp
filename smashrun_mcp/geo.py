"""GPS-Auswertung eines Smashrun-Laufs: Laufrichtung und Streckenbeschreibung mit OSM-Daten.

- route_direction(): Richtung einer Rundstrecke (Uhrzeigersinn / dagegen) aus der GPS-Spur,
  reine Geometrie (Shoelace-Formel), kein Netzwerkzugriff.
- describe_route(): ordnet die Spur per Valhalla-Map-Matching (OpenStreetMap-Daten) Wegen zu,
  Ausweichlösung Overpass-API. Liefert Abschnitte mit Namen, Wegtyp, Belag und km-Bereich.

Reine Standardbibliothek plus httpx. Läuft auch allein:  python3 geo.py LAUF.json
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger("smashrun.geo")

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
# Der MCP-Client wartet nicht ewig (0.2.1: trotz 25 s Frist Timeout beim Client). Deshalb
# gilt eine Frist je Quelle: erst Valhalla (Map-Matching), danach Overpass als Ausweichlösung.
# Zusammen höchstens ca. 15 s, danach kommt die Antwort ohne Wegbeschreibung (mit
# route_error), aber mit Laufrichtung.
VALHALLA_URL = os.environ.get("SMASHRUN_VALHALLA_URL", "https://valhalla1.openstreetmap.de/trace_attributes")
VALHALLA_DEADLINE_S = float(os.environ.get("SMASHRUN_VALHALLA_DEADLINE", "8"))
# Kurz halten: ein langer X-Client-Id-Wert hat beim Demo-Server schon Nginx zum Absturz gebracht.
VALHALLA_CLIENT_ID = "smashrun-mcp-ha"
VALHALLA_STEP_M = 15.0  # Spur vor dem Senden auf einen Punkt alle 15 m ausdünnen
OVERPASS_DEADLINE_S = float(os.environ.get("SMASHRUN_OVERPASS_DEADLINE", "7"))
OVERPASS_QUERY_TIMEOUT_S = 15
CACHE_TTL_S = 30 * 24 * 3600
CACHE_FILE = Path(os.environ.get("SMASHRUN_GEO_CACHE", "/data/overpass_cache.json"))
_mem_cache: dict[str, dict] = {}
USER_AGENT = "smashrun-mcp/0.2 (private running coach; contact via Home Assistant owner)"
EARTH_R = 6371000.0

HIGHWAY_DE = {
    "footway": "Fußweg",
    "path": "Pfad",
    "cycleway": "Radweg",
    "track": "Feldweg",
    "pedestrian": "Fußgängerzone",
    "service": "Zufahrt",
    "residential": "Wohnstraße",
    "living_street": "verkehrsberuhigte Straße",
    "tertiary": "Straße",
    "secondary": "Straße",
    "primary": "Straße",
    "unclassified": "Straße",
    "steps": "Treppe",
}
PAVED = {"asphalt", "paved", "concrete", "paving_stones", "sett", "concrete:plates", "bricks"}


# --------------------------------------------------------------------------
# Zeitreihen lesen
# --------------------------------------------------------------------------
def track_from_activity(run: dict) -> list[tuple[float, float, float]]:
    """(lat, lon, distanz_m) je Aufzeichnungspunkt. Punkte ohne GPS werden übersprungen."""
    keys, vals = run.get("recordingKeys"), run.get("recordingValues")
    if not keys or not vals:
        raise ValueError("Lauf enthält keine Zeitreihen (recordingValues).")
    col = {k: vals[i] for i, k in enumerate(keys)}
    if not {"latitude", "longitude", "distance"} <= set(col):
        raise ValueError("Zeitreihen ohne GPS (latitude/longitude/distance).")
    pts = []
    for la, lo, d in zip(col["latitude"], col["longitude"], col["distance"]):
        if la is None or lo is None or (la == 0 and lo == 0):
            continue
        pts.append((float(la), float(lo), float(d) * 1000.0))
    if len(pts) < 10:
        raise ValueError("Zu wenige GPS-Punkte für eine Auswertung.")
    return pts


def _project(lat: float, lon: float, lat0: float) -> tuple[float, float]:
    """Lokale Näherung in Metern (x nach Osten, y nach Norden)."""
    x = math.radians(lon) * EARTH_R * math.cos(math.radians(lat0))
    y = math.radians(lat) * EARTH_R
    return x, y


# --------------------------------------------------------------------------
# Laufrichtung
# --------------------------------------------------------------------------
def _direction_confidence(kind: str, norm: float, gap: float) -> str:
    """hoch: klare Schleife; mittel: schmale Schleife (z. B. Hausrunde, norm ca. 0,005);
    niedrig: alles andere. Schwellen an echten Läufen kalibriert (24.10.2026: norm 0,0052)."""
    if kind != "rundstrecke" or gap > 150:
        return "niedrig"
    if norm >= 0.005:
        return "hoch"
    return "mittel"


def route_direction(pts: list[tuple[float, float, float]]) -> dict[str, Any]:
    """Richtung einer Rundstrecke. Positive Shoelace-Fläche = gegen den Uhrzeigersinn."""
    lat0 = sum(p[0] for p in pts) / len(pts)
    xy = [_project(p[0], p[1], lat0) for p in pts]
    gap = math.dist(xy[0], xy[-1])
    area = 0.0
    for (x1, y1), (x2, y2) in zip(xy, xy[1:] + xy[:1]):
        area += x1 * y2 - x2 * y1
    area /= 2.0
    length = pts[-1][2] or 1.0
    # Normierte Fläche: Kreis = 1/(4*pi) = 0.08, Hin-und-zurück-Strecke ~ 0.
    norm = abs(area) / (length * length)
    if gap > 250:
        kind, direction = "offen", None
    elif norm < 0.004:
        kind, direction = "hin_und_zurueck", None
    else:
        kind = "rundstrecke"
        direction = "gegen den Uhrzeigersinn" if area > 0 else "im Uhrzeigersinn"
    return {
        "kind": kind,
        "direction": direction,
        "start_end_gap_m": round(gap),
        "enclosed_area_m2": round(abs(area)),
        "normalized_area": round(norm, 4),
        "confidence": _direction_confidence(kind, norm, gap),
    }


# --------------------------------------------------------------------------
# Overpass
# --------------------------------------------------------------------------
def build_query(south: float, west: float, north: float, east: float) -> str:
    bbox = f"{south:.5f},{west:.5f},{north:.5f},{east:.5f}"
    return (
        f"[out:json][timeout:{OVERPASS_QUERY_TIMEOUT_S}];("
        f'way["highway"]["highway"!~"^(motorway|motorway_link|trunk|trunk_link|proposed|construction|'
        f'raceway|bus_guideway|corridor|elevator)$"]({bbox});'
        f'way["leisure"="park"]({bbox});'
        ");out tags geom;"
    )


def _cache_key(south: float, west: float, north: float, east: float) -> str:
    raw = f"{south:.4f},{west:.4f},{north:.4f},{east:.4f}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _cache_load() -> dict[str, Any]:
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _cache_get(key: str) -> Optional[dict]:
    hit = _mem_cache.get(key)
    if hit is None:
        entry = _cache_load().get(key)
        if entry and time.time() - entry.get("t", 0) < CACHE_TTL_S:
            hit = entry["osm"]
            _mem_cache[key] = hit
    return hit


def _cache_put(key: str, osm: dict) -> None:
    _mem_cache[key] = osm
    try:
        data = _cache_load()
        now = time.time()
        data = {k: v for k, v in data.items() if now - v.get("t", 0) < CACHE_TTL_S}
        data[key] = {"t": now, "osm": osm}
        if CACHE_FILE.parent.is_dir():
            CACHE_FILE.write_text(json.dumps(data))
    except OSError as e:  # Cache ist optional
        log.warning("Overpass-Cache nicht schreibbar: %s", e)


def fetch_overpass(south: float, west: float, north: float, east: float) -> dict:
    """OSM-Wege der Bounding-Box. Zuerst Cache, dann Overpass mit Gesamtfrist."""
    key = _cache_key(south, west, north, east)
    cached = _cache_get(key)
    if cached is not None:
        log.info("Overpass-Cache-Treffer (%s)", key)
        return {**cached, "_cached": True}
    query = build_query(south, west, north, east)
    deadline = time.monotonic() + OVERPASS_DEADLINE_S
    last: Optional[Exception] = None
    for url in OVERPASS_URLS:
        remaining = deadline - time.monotonic()
        if remaining < 3:
            break
        t0 = time.monotonic()
        try:
            resp = httpx.post(
                url,
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
                timeout=httpx.Timeout(min(remaining, 20.0), connect=5.0),
            )
            log.info("Overpass %s: HTTP %s nach %.1f s", url, resp.status_code, time.monotonic() - t0)
            if resp.status_code == 200:
                osm = resp.json()
                _cache_put(key, osm)
                return osm
            last = RuntimeError(f"{url}: HTTP {resp.status_code}")
        except httpx.HTTPError as e:
            log.warning("Overpass %s: %s nach %.1f s", url, type(e).__name__, time.monotonic() - t0)
            last = e
    raise RuntimeError(f"Overpass nicht erreichbar oder zu langsam ({last}).")


# --------------------------------------------------------------------------
# Valhalla (Map-Matching): legt die Spur auf das Wegenetz und liefert Wege mit Namen,
# Wegtyp und Belag direkt zurück. Öffentlicher Demo-Server von FOSSGIS: fair use,
# 1 Anfrage/s, daher ein Aufruf pro Lauf und Ergebnis im Cache.
# --------------------------------------------------------------------------
USE_DE = {
    "footway": "Fußweg",
    "sidewalk": "Gehweg",
    "path": "Pfad",
    "cycleway": "Radweg",
    "mountain_bike": "Radweg",
    "track": "Feldweg",
    "pedestrian": "Fußgängerzone",
    "pedestrian_crossing": "Querung",
    "living_street": "verkehrsberuhigte Straße",
    "service_road": "Zufahrt",
    "driveway": "Zufahrt",
    "alley": "Gasse",
    "road": "Straße",
    "steps": "Treppe",
    "bridleway": "Reitweg",
    "rest_area": "Rastplatz",
}


def sample_shape(pts: list[tuple[float, float, float]], step_m: float = VALHALLA_STEP_M) -> list[dict]:
    """Punkte für Valhalla: erster, letzter und je einer alle step_m Meter."""
    out, next_d = [], 0.0
    for lat, lon, d in pts:
        if d >= next_d:
            out.append({"lat": round(lat, 6), "lon": round(lon, 6)})
            next_d = d + step_m
    last = {"lat": round(pts[-1][0], 6), "lon": round(pts[-1][1], 6)}
    if out[-1] != last:
        out.append(last)
    return out


def fetch_valhalla(shape: list[dict]) -> dict:
    """trace_attributes beim Valhalla-Server. Zuerst Cache, sonst ein Aufruf mit Frist."""
    key = "v" + hashlib.sha1(json.dumps(shape, separators=(",", ":")).encode()).hexdigest()[:15]
    cached = _cache_get(key)
    if cached is not None:
        log.info("Valhalla-Cache-Treffer (%s)", key)
        return {**cached, "_cached": True}
    body = {
        "shape": shape,
        "costing": "pedestrian",
        "shape_match": "map_snap",
        "trace_options": {"search_radius": 30, "gps_accuracy": 10},
        "filters": {
            "attributes": [
                "edge.names", "edge.length", "edge.surface", "edge.use", "edge.road_class",
                "edge.bridge", "edge.tunnel",
            ],
            "action": "include",
        },
    }
    t0 = time.monotonic()
    try:
        resp = httpx.post(
            VALHALLA_URL,
            json=body,
            headers={"User-Agent": USER_AGENT, "X-Client-Id": VALHALLA_CLIENT_ID},
            timeout=httpx.Timeout(VALHALLA_DEADLINE_S, connect=4.0),
        )
    except httpx.HTTPError as e:
        log.warning("Valhalla: %s nach %.1f s", type(e).__name__, time.monotonic() - t0)
        raise RuntimeError(f"Valhalla nicht erreichbar oder zu langsam ({type(e).__name__}).")
    log.info("Valhalla %s: HTTP %s nach %.1f s", VALHALLA_URL, resp.status_code, time.monotonic() - t0)
    if resp.status_code != 200:
        detail = ""
        try:
            detail = str(resp.json().get("error", ""))[:120]
        except ValueError:
            pass
        raise RuntimeError(f"Valhalla: HTTP {resp.status_code} {detail}".strip())
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError("Valhalla: Antwort ist kein JSON.")
    if not data.get("edges"):
        raise RuntimeError("Valhalla: keine Wege zur Spur gefunden.")
    slim = {"edges": data["edges"]}  # nur das Nötige in den Cache
    _cache_put(key, slim)
    return slim


def match_valhalla(pts: list[tuple[float, float, float]], vresp: dict) -> dict[str, Any]:
    """Valhalla-Kanten zu Abschnitten mit km-Bereich (auf die aufgezeichnete Strecke skaliert)."""
    edges = []
    for e in vresp.get("edges", []):
        length_m = float(e.get("length") or 0.0) * 1000.0  # Valhalla liefert km
        if length_m <= 0:
            continue
        names = e.get("names") or []
        use = e.get("use") or ""
        kind = USE_DE.get(use, use or "Weg")
        label = names[0] if names else f"{kind} ohne Namen"
        edges.append(
            {
                "label": label, "type": kind, "length_m": length_m,
                "surface": e.get("surface"), "bridge": bool(e.get("bridge")),
            }
        )
    if not edges:
        raise RuntimeError("Valhalla: keine auswertbaren Wege.")
    matched_total = sum(e["length_m"] for e in edges)
    total = pts[-1][2]
    scale = total / matched_total if matched_total else 1.0

    # Gleiche Bezeichnung in Folge zusammenfassen, sehr kurze Kanten (< 30 m) dem vorigen zuschlagen.
    groups: list[dict[str, Any]] = []
    for e in edges:
        if groups and (groups[-1]["label"] == e["label"] or e["length_m"] < 30):
            groups[-1]["edges"].append(e)
        else:
            groups.append({"label": e["label"], "edges": [e]})

    out, surface_m, pos = [], {"befestigt": 0.0, "unbefestigt": 0.0, "unbekannt": 0.0}, 0.0
    for g in groups:
        length = sum(e["length_m"] for e in g["edges"]) * scale
        # Typ und Belag nach dem längsten Anteil der Kanten bestimmen
        by_type: dict[str, float] = {}
        by_surf: dict[Optional[str], float] = {}
        for e in g["edges"]:
            by_type[e["type"]] = by_type.get(e["type"], 0.0) + e["length_m"]
            by_surf[e["surface"]] = by_surf.get(e["surface"], 0.0) + e["length_m"]
        kind_de = max(by_type, key=by_type.get)
        surface = max(by_surf, key=by_surf.get)
        if surface and surface.startswith("paved"):
            sclass = "befestigt"
        elif surface:
            sclass = "unbefestigt"
        else:
            sclass = "unbekannt"
        surface_m[sclass] += length
        bridge_m = sum(e["length_m"] for e in g["edges"] if e["bridge"])
        out.append(
            {
                "from_km": round(pos / 1000, 2),
                "to_km": round((pos + length) / 1000, 2),
                "length_m": round(length),
                "name": g["label"],
                "type": kind_de,
                "surface": surface,
                "bridge": "yes" if bridge_m >= 20 else None,
                "park": None,  # Valhalla liefert keine Parkflächen
            }
        )
        pos += length
    return {
        "stretches": out,
        "surface_share_pct": {k: round(100 * v / total) for k, v in surface_m.items()} if total else {},
        "matched_vs_recorded": round(matched_total / total, 2) if total else None,
    }


def _seg_dist(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _in_polygon(px: float, py: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _label(tags: dict) -> str:
    name = tags.get("name") or tags.get("ref")
    kind = HIGHWAY_DE.get(tags.get("highway", ""), tags.get("highway", "Weg"))
    return name if name else f"{kind} ohne Namen"


def match_route(
    pts: list[tuple[float, float, float]], osm: dict, max_dist_m: float = 25.0, step_m: float = 25.0
) -> dict[str, Any]:
    """Ordnet die Spur den OSM-Wegen zu und fasst sie zu Abschnitten zusammen."""
    lat0 = sum(p[0] for p in pts) / len(pts)
    ways, parks = [], []
    for el in osm.get("elements", []):
        geom = el.get("geometry") or []
        if el.get("type") != "way" or len(geom) < 2:
            continue
        xy = [_project(g["lat"], g["lon"], lat0) for g in geom]
        tags = el.get("tags", {})
        if "highway" in tags:
            ways.append((tags, xy))
        elif tags.get("leisure") == "park" and xy[0] == xy[-1]:
            parks.append((tags.get("name", "Park ohne Namen"), xy))

    # Spur gleichmäßig alle step_m Meter abtasten.
    samples, next_d = [], 0.0
    for lat, lon, d in pts:
        if d >= next_d:
            samples.append((d, _project(lat, lon, lat0)))
            next_d = d + step_m

    labelled = []
    for d, (px, py) in samples:
        best, best_dist = None, max_dist_m
        for tags, xy in ways:
            for a, b in zip(xy, xy[1:]):
                dist = _seg_dist(px, py, a[0], a[1], b[0], b[1])
                if dist < best_dist:
                    best, best_dist = tags, dist
        park = next((n for n, poly in parks if _in_polygon(px, py, poly)), None)
        if best is None:
            labelled.append((d, "kein Weg in OSM gefunden", {}, park))
        else:
            labelled.append((d, _label(best), best, park))

    # Gleiche Namen in Folge zu Abschnitten verbinden.
    stretches: list[dict[str, Any]] = []
    for d, label, tags, park in labelled:
        if stretches and stretches[-1]["name"] == label:
            stretches[-1]["to_m"] = d
            stretches[-1]["_parks"].append(park)
            stretches[-1]["_tags"].append(tags)
        else:
            stretches.append({"name": label, "from_m": d, "to_m": d, "_parks": [park], "_tags": [tags]})

    # Sehr kurze Abschnitte (< 60 m) dem vorigen zuschlagen, damit GPS-Rauschen nicht stückelt.
    merged: list[dict[str, Any]] = []
    for s in stretches:
        if merged and (s["to_m"] - s["from_m"]) < 60:
            merged[-1]["to_m"] = s["to_m"]
            merged[-1]["_parks"] += s["_parks"]
            merged[-1]["_tags"] += s["_tags"]
        elif merged and merged[-1]["name"] == s["name"]:
            merged[-1]["to_m"] = s["to_m"]
            merged[-1]["_parks"] += s["_parks"]
            merged[-1]["_tags"] += s["_tags"]
        else:
            merged.append(s)

    out, surface_m = [], {"befestigt": 0.0, "unbefestigt": 0.0, "unbekannt": 0.0}
    total = pts[-1][2]
    for i, s in enumerate(merged):
        end = merged[i + 1]["from_m"] if i + 1 < len(merged) else total
        start = 0.0 if i == 0 else s["from_m"]
        length = max(0.0, end - start)
        tags = max(s["_tags"], key=lambda t: len(t)) if s["_tags"] else {}
        surface = tags.get("surface")
        if surface in PAVED:
            kind = "befestigt"
        elif surface:
            kind = "unbefestigt"
        else:
            kind = "unbekannt"
        surface_m[kind] += length
        parks_seen = [p for p in s["_parks"] if p]
        park = max(set(parks_seen), key=parks_seen.count) if parks_seen else None
        if park and parks_seen.count(park) * 2 <= len(s["_parks"]):
            park = None  # nur wenn die Mehrheit des Abschnitts im Park liegt
        out.append(
            {
                "from_km": round(start / 1000, 2),
                "to_km": round(end / 1000, 2),
                "length_m": round(length),
                "name": s["name"],
                "type": HIGHWAY_DE.get(tags.get("highway", ""), tags.get("highway")),
                "surface": surface,
                "bridge": tags.get("bridge") or None,
                "park": park,
            }
        )
    return {
        "stretches": out,
        "surface_share_pct": {k: round(100 * v / total) for k, v in surface_m.items()} if total else {},
        "ways_in_osm": len(ways),
    }


def describe_route(run: dict, fetch=fetch_overpass, valhalla=fetch_valhalla) -> dict[str, Any]:
    """Richtung plus Streckenbeschreibung eines Laufs. fetch/valhalla sind für Tests austauschbar.

    Erst Valhalla (Map-Matching), bei Fehler oder Zeitüberschreitung Overpass als Ausweichlösung.
    """
    pts = track_from_activity(run)
    res: dict[str, Any] = {"direction": route_direction(pts)}
    v_error = None
    t_v = time.monotonic()
    try:
        vresp = valhalla(sample_shape(pts))
        v_cached = bool(vresp.pop("_cached", False))
        res.update(match_valhalla(pts, vresp))
        res["timing_s"] = {"valhalla": round(time.monotonic() - t_v, 1), "from_cache": v_cached}
        res["source"] = "OpenStreetMap-Mitwirkende (ODbL), Map-Matching über Valhalla (FOSSGIS-Demo-Server)"
        res["note"] = (
            "Belag nach Valhalla (teils aus dem Wegtyp abgeleitet). Keine Parkflächen, keine Höhen."
        )
        return res
    except RuntimeError as e:
        v_error = str(e)
        log.warning("Valhalla ohne Ergebnis, weiter mit Overpass: %s", v_error)
    res["valhalla_error"] = v_error
    pad = 0.0006  # ca. 65 m Rand um die Spur
    south = min(p[0] for p in pts) - pad
    north = max(p[0] for p in pts) + pad
    west = min(p[1] for p in pts) - pad
    east = max(p[1] for p in pts) + pad
    t0 = time.monotonic()
    try:
        osm = fetch(south, west, north, east)
    except RuntimeError as e:
        res["route_error"] = f"{v_error.rstrip('.')}; Overpass: {e}"
        res["timing_s"] = {
            "valhalla": round(t0 - t_v, 1),
            "overpass": round(time.monotonic() - t0, 1),
        }
        return res
    t1 = time.monotonic()
    cached = bool(osm.pop("_cached", False))
    res.update(match_route(pts, osm))
    res["timing_s"] = {
        "valhalla": round(t0 - t_v, 1),
        "overpass": round(t1 - t0, 1),
        "matching": round(time.monotonic() - t1, 1),
        "from_cache": cached,
    }
    res["source"] = "OpenStreetMap-Mitwirkende (ODbL), Abfrage per Overpass-API"
    res["note"] = "Höhen liefert OSM nicht. Belag nur, wo er in OSM eingetragen ist."
    return res


if __name__ == "__main__":
    with open(sys.argv[1]) as f:
        print(json.dumps(describe_route(json.load(f)), indent=2, ensure_ascii=False))
