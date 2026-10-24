#!/usr/bin/env python3
"""Smashrun MCP server.

Gibt Claude Zugriff auf die eigenen Smashrun-Daten (Läufe, Splits, Statistiken,
Ziele, Gewicht, Profil). Schreibende Tools (Lauf anlegen/ändern/löschen, Notiz,
Gewicht) sind standardmäßig aktiv und brauchen ein Token mit Scope write_activity.
Mit SMASHRUN_ALLOW_WRITE=0 läuft der Server rein lesend.

Authentifizierung (in dieser Reihenfolge):
  1. Umgebungsvariable SMASHRUN_ACCESS_TOKEN
  2. Token-Datei (Standard: ~/.config/smashrun-mcp/token.json, siehe auth.py)
Optional für automatische Erneuerung: SMASHRUN_CLIENT_ID + SMASHRUN_CLIENT_SECRET
(nur mit eigenem, bei Smashrun registriertem API-Key; Refresh-Token stammt aus auth.py).
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import httpx

import analysis  # lokale Auswertung der Zeitreihen (analysis.py)
import geo  # Laufrichtung und OSM-Streckenbeschreibung (geo.py)
try:  # MCP SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # MCP SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server

API = "https://api.smashrun.com/v1"
TOKEN_URL = "https://secure.smashrun.com/oauth2/token"
TOKEN_FILE = Path(
    os.environ.get(
        "SMASHRUN_TOKEN_FILE",
        Path.home() / ".config" / "smashrun-mcp" / "token.json",
    )
)
# Schreibzugriff ist standardmäßig an; mit SMASHRUN_ALLOW_WRITE=0 wird der Server rein lesend.
ALLOW_WRITE = os.environ.get("SMASHRUN_ALLOW_WRITE", "1").lower() not in ("0", "false", "no", "off")
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

mcp = _Server("smashrun")


class SmashrunError(Exception):
    pass


# --------------------------------------------------------------------------
# Token-Verwaltung
# --------------------------------------------------------------------------
def _load_token_file() -> dict[str, Any]:
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_token_file(data: dict[str, Any]) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(data, indent=2))
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass


def _refresh_token() -> Optional[str]:
    """Erneuert das Access-Token per Refresh-Token. Nur mit eigenem API-Key möglich."""
    data = _load_token_file()
    client_id = os.environ.get("SMASHRUN_CLIENT_ID")
    client_secret = os.environ.get("SMASHRUN_CLIENT_SECRET")
    refresh = data.get("refresh_token")
    if not (client_id and client_secret and refresh):
        return None
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        return None
    new = resp.json()
    data["access_token"] = new["access_token"]
    data["expires_at"] = time.time() + int(new.get("expires_in", 0) or 0)
    if new.get("refresh_token"):
        data["refresh_token"] = new["refresh_token"]
    _save_token_file(data)
    return data["access_token"]


def _get_token() -> str:
    env = os.environ.get("SMASHRUN_ACCESS_TOKEN")
    if env:
        return env
    data = _load_token_file()
    token = data.get("access_token")
    expires_at = data.get("expires_at")
    if token and expires_at and time.time() > float(expires_at) - 60:
        token = _refresh_token() or token
    if not token:
        raise SmashrunError(
            "Kein Smashrun-Token gefunden. SMASHRUN_ACCESS_TOKEN setzen oder "
            "'python auth.py --token <TOKEN>' ausführen (siehe README)."
        )
    return token


# --------------------------------------------------------------------------
# HTTP-Hilfsfunktionen
# --------------------------------------------------------------------------
def _request(method: str, path: str, *, params: dict | None = None, body: Any = None) -> Any:
    url = f"{API}{path}"
    for attempt in range(2):
        token = _get_token()
        resp = httpx.request(
            method,
            url,
            params=params,
            json=body,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=TIMEOUT,
        )
        if resp.status_code == 401 and attempt == 0 and not os.environ.get("SMASHRUN_ACCESS_TOKEN"):
            if _refresh_token():
                continue
        break
    if resp.status_code == 401:
        raise SmashrunError(
            "Smashrun hat das Token abgelehnt (abgelaufen oder widerrufen). "
            "Neues Token holen (API Explorer, Client-ID 'client'). check_token zeigt die Restlaufzeit."
        )
    if resp.status_code == 429:
        raise SmashrunError("Smashrun-Rate-Limit erreicht (User-Level: 250 Anfragen/Stunde).")
    if resp.status_code >= 400:
        raise SmashrunError(f"Smashrun-API-Fehler {resp.status_code}: {resp.text[:300]}")
    if not resp.content:
        return {"ok": True}
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


def _get(path: str, params: dict | None = None) -> Any:
    return _request("GET", path, params=params)


def _fmt_pace(duration_s: float, distance_km: float) -> Optional[str]:
    if not distance_km or distance_km <= 0 or not duration_s:
        return None
    sec = duration_s / distance_km
    return f"{int(sec // 60)}:{int(round(sec % 60)):02d}"


def _enrich(run: Any) -> Any:
    """Fügt Pace (min/km) hinzu, wenn Distanz und Dauer vorhanden sind, und dekodiert Notizen."""
    if isinstance(run, dict):
        # Smashrun liefert Notizen HTML-kodiert zurück (z. B. 'gr&uuml;n'); lesbar machen.
        if isinstance(run.get("notes"), str):
            run["notes"] = html.unescape(run["notes"])
        dist, dur = run.get("distance"), run.get("duration")
        if isinstance(dist, (int, float)) and isinstance(dur, (int, float)):
            pace = _fmt_pace(dur, dist)
            if pace:
                run["pace_min_per_km"] = pace
    return run


def _parse_day(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SmashrunError(f"{name} muss im Format YYYY-MM-DD angegeben werden.")


def _run_date(run: dict) -> Optional[date]:
    s = run.get("startDateTimeLocal")
    if not isinstance(s, str) or len(s) < 10:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Lesende Tools
# --------------------------------------------------------------------------
@mcp.tool()
def get_userinfo() -> Any:
    """Profil des angemeldeten Smashrun-Nutzers: Name, Einheiten, Ruhe-/Maximalpuls,
    Zeitpunkt des letzten Laufs und der letzten Datenänderung."""
    return _get("/my/userinfo")


@mcp.tool()
def get_activities(
    mode: Literal["summary", "briefs", "extended", "ids"] = "summary",
    count: int = 20,
    page: int = 0,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> Any:
    """Läufe auflisten (neueste zuerst). Distanzen in km, Dauer in Sekunden.

    mode: 'summary' (Standard), 'briefs' (nur id/Start/Distanz/Dauer, sparsam),
          'extended' (zusätzlich Höhenmeter, Ort, Leistung, Sonnenstand ...),
          'ids' (nur Lauf-IDs).
    count: Läufe pro Seite (1-100). page: Seite, bei 0 beginnend.
    since / until: Datumsfilter 'YYYY-MM-DD' auf das Lauf-Datum (lokale Startzeit).
    Hinweis: Mit Datumsfilter werden die Seiten clientseitig gefiltert; bei großen
    Zeiträumen lieber get_stats nutzen, das schont das Rate-Limit.
    """
    count = max(1, min(int(count), 100))
    page = max(0, int(page))
    suffix = {"summary": "", "briefs": "/briefs", "extended": "/extended", "ids": "/ids"}[mode]
    params: dict[str, Any] = {"count": count, "page": page}
    since_d = _parse_day(since, "since") if since else None
    until_d = _parse_day(until, "until") if until else None
    if since_d:
        epoch = datetime(since_d.year, since_d.month, since_d.day, tzinfo=timezone.utc).timestamp()
        params["fromDateUTC"] = int(epoch) - 86400  # Puffer, exakt wird unten gefiltert
    runs = _get(f"/my/activities/search{suffix}", params)
    if mode == "ids" or not isinstance(runs, list):
        return runs
    out = []
    for r in runs:
        d = _run_date(r) if isinstance(r, dict) else None
        if d and since_d and d < since_d:
            continue
        if d and until_d and d > until_d:
            continue
        out.append(_enrich(r))
    return {"page": page, "count_requested": count, "returned": len(out), "runs": out}


@mcp.tool()
def get_activity(
    activity_id: int, include_recordings: bool = False, max_points: int = 200
) -> Any:
    """Vollständige Details eines Laufs (Summary, Laps, Wetter, Notizen, Pausen ...).

    include_recordings: Zeitreihen (Herzfrequenz, GPS, Distanz ...) mitliefern; sie werden
    auf max_points gleichmäßig ausgedünnt, damit die Antwort handlich bleibt.
    """
    run = _get(f"/my/activities/{int(activity_id)}")
    if not isinstance(run, dict):
        return run
    values = run.get("recordingValues")
    if isinstance(values, list):
        total = len(values)
        if include_recordings and total > max(10, int(max_points)):
            step = total / max(10, int(max_points))
            run["recordingValues"] = [values[int(i * step)] for i in range(int(max_points))]
            run["recordingsDownsampledFrom"] = total
        elif not include_recordings:
            run.pop("recordingValues", None)
            run.pop("recordingKeys", None)
            run["recordingsOmitted"] = total
    return _enrich(run)


@mcp.tool()
def get_activity_splits(activity_id: int, unit: Literal["km", "mi"] = "km") -> Any:
    """Kilometer- oder Meilen-Splits eines Laufs (Tempo, ggf. Herzfrequenz)."""
    return _get(f"/my/activities/{int(activity_id)}/splits/{unit}")


@mcp.tool()
def analyze_activity(
    activity_id: int,
    effort_threshold_pace: Optional[str] = None,
    window_s: int = 12,
) -> Any:
    """Wertet einen Lauf sekundengenau aus den Zeitreihen aus (rein lesend, 1 API-Aufruf).

    Liefert kompakt: km-Splits mit Puls, Antritte (Start-km, Länge, Pace, Puls), Negative-Split-Analyse
    (erste vs. zweite Hälfte, auch ohne den ersten Kilometer), Split-Streuung, Puls-Drift
    (Tempo pro Puls, zweite vs. erste Hälfte), Puls- und Kadenz-Kennzahlen.

    effort_threshold_pace: feste Schwelle für Antritte, z. B. '5:20'. Standard: 12 % schneller als
    das Median-Tempo des Laufs. window_s: Glättungsfenster in Sekunden (Standard 12).
    Läufe ohne Zeitreihen (manuell angelegt) können nicht ausgewertet werden.
    """
    run = _get(f"/my/activities/{int(activity_id)}")
    if not isinstance(run, dict):
        return run
    try:
        res = analysis.analyze_activity_json(
            run, thr_pace=effort_threshold_pace, window_s=max(4, min(int(window_s), 60))
        )
    except ValueError as e:
        raise SmashrunError(str(e))
    res["activityId"] = int(activity_id)
    res["startDateTimeLocal"] = run.get("startDateTimeLocal")
    try:  # Laufrichtung aus der GPS-Spur, rein geometrisch, ohne Netzwerk
        res["route_direction"] = geo.route_direction(geo.track_from_activity(run))
    except ValueError:
        res["route_direction"] = None
    return res


@mcp.tool()
def describe_route(activity_id: int) -> Any:
    """Beschreibt die Strecke eines Laufs mit OpenStreetMap-Daten (rein lesend).

    Liefert die Laufrichtung (Uhrzeigersinn oder dagegen) und die Abschnitte der Strecke mit
    km-Bereich, Wegname, Wegtyp (Fußweg, Radweg, Straße ...), Belag (wo in OSM eingetragen),
    Brücke und Park. Ordnet dazu die GPS-Spur per Overpass-API den OSM-Wegen zu (ein Aufruf
    für die ganze Strecke). Höhen liefert OSM nicht. Ist Overpass nicht erreichbar, kommt
    trotzdem die Laufrichtung zurück, plus eine Fehlermeldung unter route_error.
    """
    run = _get(f"/my/activities/{int(activity_id)}")
    if not isinstance(run, dict):
        return run
    try:
        res = geo.describe_route(run)
    except ValueError as e:
        raise SmashrunError(str(e))
    res["activityId"] = int(activity_id)
    res["startDateTimeLocal"] = run.get("startDateTimeLocal")
    return res


@mcp.tool()
def get_activity_tags(activity_id: int) -> Any:
    """Tags eines Laufs (eingebaute wie 'long run' und eigene)."""
    return _get(f"/my/activities/{int(activity_id)}/tags")


@mcp.tool()
def get_activity_notables(activity_id: int) -> Any:
    """Besonderheiten eines Laufs im Vergleich zu früheren Läufen (z. B. schnellste 5 km in 3 Monaten)."""
    return _get(f"/my/activities/{int(activity_id)}/notables")


@mcp.tool()
def get_stats(year: Optional[int] = None, month: Optional[int] = None) -> Any:
    """Aggregierte Statistiken: Anzahl Läufe, Gesamtdistanz, Durchschnittstempo, längster Lauf,
    längste Serie ... Ohne Argumente: gesamte Laufbahn; mit year: ein Jahr; mit year+month: ein Monat."""
    if month is not None and year is None:
        raise SmashrunError("Für month muss auch year angegeben werden.")
    path = "/my/stats"
    if year is not None:
        path += f"/{int(year)}"
        if month is not None:
            path += f"/{int(month)}"
    return _get(path)


@mcp.tool()
def get_goals(year: int, month: Optional[int] = None) -> Any:
    """Ziel und Fortschritt für ein Jahr oder (mit month) einen Monat. null, wenn kein Ziel gesetzt ist."""
    path = f"/my/goals/{int(year)}"
    if month is not None:
        path += f"/{int(month)}"
    return _get(path)


@mcp.tool()
def get_weight(latest_only: bool = False) -> Any:
    """Gewichtsverlauf in kg (oder nur die letzte Messung)."""
    return _get("/my/body/weight/latest" if latest_only else "/my/body/weight")


@mcp.tool()
def check_token() -> Any:
    """Prüft das aktuelle Token: Scopes und verbleibende Gültigkeit (das Token selbst wird nicht ausgegeben)."""
    info = _get(f"/auth/{_get_token()}")
    if isinstance(info, dict):
        info.pop("token", None)
        if isinstance(info.get("expires_in"), (int, float)):
            info["expires_in_days"] = round(info["expires_in"] / 86400, 1)
    return info


# --------------------------------------------------------------------------
# Schreibende Tools (nur mit SMASHRUN_ALLOW_WRITE=1)
# --------------------------------------------------------------------------
if ALLOW_WRITE:

    @mcp.tool()
    def update_run_notes(activity_id: int, notes: str) -> Any:
        """Ändert die Notiz (Beschreibung) eines Laufs. Überschreibt die bisherige Notiz. Max. 800 Zeichen.
        Benötigt den Scope write_activity."""
        if len(notes) > 800:
            raise SmashrunError("Notizen dürfen höchstens 800 Zeichen lang sein.")
        return _request(
            "PATCH",
            f"/my/activities/{int(activity_id)}",
            body={"activityId": int(activity_id), "notes": notes},
        )

    @mcp.tool()
    def add_weight(weight_kg: float, day: Optional[str] = None) -> Any:
        """Trägt ein Gewicht (kg) ein, optional für ein Datum 'YYYY-MM-DD' (Standard: heute)."""
        if not 20 <= weight_kg <= 400:
            raise SmashrunError("Gewicht außerhalb des plausiblen Bereichs (20-400 kg).")
        body: dict[str, Any] = {"weightInKilograms": weight_kg}
        if day:
            body["date"] = _parse_day(day, "day").isoformat()
        return _request("POST", "/my/body/weight", body=body)

    @mcp.tool()
    def create_run(
        start: str,
        distance_km: float,
        duration_s: int,
        notes: Optional[str] = None,
        terrain: Optional[Literal["road", "trail", "track", "treadmill", "beach", "snowpack"]] = None,
        how_felt: Optional[Literal["Unstoppable", "great", "soso", "tired", "injured"]] = None,
        weather: Optional[Literal["indoor", "clear", "cloudy", "rain", "storm", "snow"]] = None,
        temperature_c: Optional[int] = None,
        heart_rate_average: Optional[int] = None,
        heart_rate_max: Optional[int] = None,
        body_weight_kg: Optional[float] = None,
        external_id: Optional[str] = None,
    ) -> Any:
        """Legt einen Lauf manuell an (nur Zusammenfassung, keine GPS-Daten).

        start: lokale Startzeit mit Zeitzonen-Offset, ISO 8601, z. B. '2026-10-12T07:30:00+02:00'.
        distance_km: Distanz in Kilometern. duration_s: reine Laufzeit in Sekunden (ohne Pausen).
        Smashrun erkennt Duplikate über identische Startzeit; ein bereits vorhandener Lauf
        einer anderen Quelle wird dabei NICHT überschrieben (Anfrage wird ignoriert).
        """
        try:
            datetime.fromisoformat(start)
        except ValueError:
            raise SmashrunError("start muss ISO 8601 sein, z. B. 2026-10-12T07:30:00+02:00.")
        if "+" not in start[10:] and "-" not in start[10:] and not start.endswith("Z"):
            raise SmashrunError("start braucht einen Zeitzonen-Offset, z. B. +02:00.")
        if not 0 < distance_km <= 500:
            raise SmashrunError("distance_km muss zwischen 0 und 500 liegen.")
        if not 0 < duration_s <= 7 * 86400:
            raise SmashrunError("duration_s außerhalb des plausiblen Bereichs.")
        if notes and len(notes) > 800:
            raise SmashrunError("Notizen dürfen höchstens 800 Zeichen lang sein.")
        body: dict[str, Any] = {
            "startDateTimeLocal": start,
            "distance": distance_km,
            "duration": int(duration_s),
            "activityType": "running",
        }
        optional = {
            "notes": notes,
            "terrain": terrain,
            "howFelt": how_felt,
            "weatherType": weather,
            "temperature": temperature_c,
            "heartRateAverage": heart_rate_average,
            "heartRateMax": heart_rate_max,
            "bodyWeight": body_weight_kg,
            "externalId": external_id,
        }
        body.update({k: v for k, v in optional.items() if v is not None})
        return _request("POST", "/my/activities/", body=body)

    @mcp.tool()
    def update_run(
        activity_id: int,
        notes: Optional[str] = None,
        distance_km: Optional[float] = None,
        duration_s: Optional[int] = None,
        start: Optional[str] = None,
    ) -> Any:
        """Ändert Felder eines bestehenden Laufs. Smashrun erlaubt per PATCH nur: notes,
        distance_km, duration_s und start (lokale Startzeit mit Offset, ISO 8601).
        Nicht angegebene Felder bleiben unverändert."""
        body: dict[str, Any] = {"activityId": int(activity_id)}
        if notes is not None:
            if len(notes) > 800:
                raise SmashrunError("Notizen dürfen höchstens 800 Zeichen lang sein.")
            body["notes"] = notes
        if distance_km is not None:
            if not 0 < distance_km <= 500:
                raise SmashrunError("distance_km muss zwischen 0 und 500 liegen.")
            body["distance"] = distance_km
        if duration_s is not None:
            if not 0 < duration_s <= 7 * 86400:
                raise SmashrunError("duration_s außerhalb des plausiblen Bereichs.")
            body["duration"] = int(duration_s)
        if start is not None:
            try:
                datetime.fromisoformat(start)
            except ValueError:
                raise SmashrunError("start muss ISO 8601 sein, z. B. 2026-10-12T07:30:00+02:00.")
            body["startDateTimeLocal"] = start
        if len(body) == 1:
            raise SmashrunError("Keine zu ändernden Felder angegeben.")
        return _request("PATCH", f"/my/activities/{int(activity_id)}", body=body)

    @mcp.tool()
    def delete_run(activity_id: int, confirm: bool = False) -> Any:
        """Löscht einen Lauf (Smashrun markiert ihn als gelöscht). Nur mit confirm=True,
        und erst nachdem der Nutzer das ausdrücklich bestätigt hat."""
        if not confirm:
            raise SmashrunError(
                "Löschen nicht bestätigt. Erst den Nutzer fragen, dann mit confirm=True erneut aufrufen."
            )
        return _request("DELETE", f"/my/activities/{int(activity_id)}")

    ENRICH_TAG = "[Coach]"
    ENRICH_FIELDS = [
        ("shoes", "Schuhe"),
        ("clothing", "Kleidung"),
        ("weather", "Wetter"),
        ("felt", "Gefühl"),
        ("extra", "Sonstiges"),
    ]

    def _split_enrichment(notes: Optional[str]) -> tuple[str, dict[str, str]]:
        """Trennt die eigene Notiz vom [Coach]-Block und liest dessen Felder."""
        kept, block = [], None
        for line in (notes or "").split("\n"):
            if line.startswith(ENRICH_TAG):
                block = line
            else:
                kept.append(line)
        fields: dict[str, str] = {}
        if block:
            for part in block[len(ENRICH_TAG):].split(" | "):
                key, sep, value = part.strip().partition(": ")
                if sep and key:
                    fields[key] = value.strip()
        return "\n".join(kept).rstrip(), fields

    @mcp.tool()
    def set_run_enrichment(
        activity_id: int,
        shoes: Optional[str] = None,
        clothing: Optional[str] = None,
        weather: Optional[str] = None,
        felt: Optional[str] = None,
        extra: Optional[str] = None,
    ) -> Any:
        """Reichert einen Lauf mit Zusatzdaten an, die Smashrun nicht als Feld kennt
        (Schuhe, Kleidung, Wetter, Befinden). Die Daten landen als eine Zeile in der Notiz:
        '[Coach] Schuhe: ... | Kleidung: ... | Wetter: ... | Gefühl: ... | Sonstiges: ...'

        Die eigene Notiz des Nutzers bleibt unverändert erhalten. Wiederholte Aufrufe
        aktualisieren nur die übergebenen Felder, bereits gesetzte Felder bleiben stehen.
        Ein leerer String ('') entfernt ein Feld. Notizen sind auf 800 Zeichen begrenzt."""
        run = _get(f"/my/activities/{int(activity_id)}")
        current = run.get("notes") if isinstance(run, dict) else None
        # Smashrun gibt Notizen HTML-kodiert zurück; ohne Dekodieren würde beim Zurückschreiben
        # doppelt kodiert ('&amp;uuml;').
        current = html.unescape(current) if isinstance(current, str) else current
        base, fields = _split_enrichment(current)
        given = {"shoes": shoes, "clothing": clothing, "weather": weather, "felt": felt, "extra": extra}
        for arg, label in ENRICH_FIELDS:
            value = given[arg]
            if value is None:
                continue
            value = " ".join(value.replace("|", "/").split())
            if value:
                fields[label] = value
            else:
                fields.pop(label, None)
        order = [label for _, label in ENRICH_FIELDS]
        parts = [f"{k}: {fields[k]}" for k in order if k in fields]
        parts += [f"{k}: {v}" for k, v in fields.items() if k not in order]
        new_notes = base
        if parts:
            block = f"{ENRICH_TAG} " + " | ".join(parts)
            new_notes = f"{base}\n{block}" if base else block
        if len(new_notes) > 800:
            raise SmashrunError(
                f"Notiz wäre {len(new_notes)} Zeichen lang (Maximum 800). Angaben kürzen."
            )
        result = _request(
            "PATCH",
            f"/my/activities/{int(activity_id)}",
            body={"activityId": int(activity_id), "notes": new_notes},
        )
        return {"activityId": int(activity_id), "notes": new_notes, "api": result}


if __name__ == "__main__":
    print(
        f"smashrun-mcp gestartet (Schreibzugriff: {'an' if ALLOW_WRITE else 'aus'})",
        file=sys.stderr,
    )
    mcp.run()
