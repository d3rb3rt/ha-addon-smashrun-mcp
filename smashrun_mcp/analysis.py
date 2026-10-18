"""Sekundengenaue Auswertung eines Smashrun-Laufs aus den Zeitreihen (recordingKeys/-Values).

Reine Python-Standardbibliothek. Liefert km-Splits, Antritte, Negative-Split-Analyse,
Puls-Drift und Kennzahlen. Wird von server.py (Tool analyze_activity) benutzt, läuft aber
auch allein:  python3 analysis.py LAUF.json
"""
from __future__ import annotations

import bisect
import json
import sys
from typing import Any, Optional


def _fmt(sec: float) -> str:
    m, s = divmod(int(round(sec)), 60)
    return f"{m}:{s:02d}"


def _pace(v_ms: float) -> str:
    return _fmt(1000 / v_ms) if v_ms > 0 else "-"


def parse_pace(p: str) -> float:
    """'5:20' -> m/s"""
    m, s = p.split(":")
    return 1000 / (int(m) * 60 + int(s))


def _interp(xs: list[float], ys: list[float], x: float) -> float:
    i = bisect.bisect_left(xs, x)
    if i <= 0:
        return ys[0]
    if i >= len(xs):
        return ys[-1]
    if xs[i] == xs[i - 1]:
        return ys[i]
    f = (x - xs[i - 1]) / (xs[i] - xs[i - 1])
    return ys[i - 1] + f * (ys[i] - ys[i - 1])


def series_from_activity(run: dict) -> tuple[list[float], list[float], list, list]:
    """Zeitreihen aus einem Smashrun-Lauf (JSON von /my/activities/{id}) lesen.
    Rückgabe: clock (s), distance (m), heartRate, cadence (ein Bein)."""
    keys, vals = run.get("recordingKeys"), run.get("recordingValues")
    if not keys or not vals:
        raise ValueError("Lauf enthält keine Zeitreihen (recordingValues).")
    col = {k: vals[i] for i, k in enumerate(keys)}
    if "clock" not in col or "distance" not in col:
        raise ValueError("Zeitreihen ohne clock/distance, keine Auswertung möglich.")
    n = len(col["clock"])
    T = [float(x) for x in col["clock"]]
    D = [float(x) * 1000 for x in col["distance"]]  # km -> m
    H = [int(x) if x else None for x in col.get("heartRate", [None] * n)]
    C = [int(x) if x else None for x in col.get("cadence", [None] * n)]
    return T, D, H, C


def analyze(
    T: list[float],
    D: list[float],
    H: list,
    C: list,
    thr_pace: Optional[str] = None,
    window_s: int = 12,
    rel: float = 1.12,
    min_len_m: int = 150,
    skip_start_s: int = 90,
    merge_gap_s: int = 10,
) -> dict[str, Any]:
    if len(T) < 20 or D[-1] < 500:
        raise ValueError("Zu wenige Messpunkte oder zu kurze Distanz für eine Auswertung.")
    N = int(T[-1])
    W = window_s
    dist_at = lambda s: _interp(T, D, s)
    time_at = lambda d: _interp(D, T, d)

    def hr_between(a: float, b: float) -> list[int]:
        return [h for t, h in zip(T, H) if a <= t <= b and h]

    out: dict[str, Any] = {
        "distance_km": round(D[-1] / 1000, 2),
        "duration": _fmt(T[-1]),
        "avg_pace": _pace(D[-1] / T[-1]),
    }

    # --- km-Splits (Rest-Kilometer am Ende mit eigener Distanz) ---
    splits = []
    full = int(D[-1] // 1000)
    for k in range(1, full + 1):
        a, b = time_at((k - 1) * 1000), time_at(k * 1000)
        hh = hr_between(a, b)
        splits.append({"km": k, "pace": _fmt(b - a), "sec": round(b - a, 1),
                       "hr_avg": round(sum(hh) / len(hh)) if hh else None,
                       "hr_max": max(hh) if hh else None})
    rest = D[-1] - full * 1000
    if rest >= 200:
        a = time_at(full * 1000)
        splits.append({"km": round(D[-1] / 1000, 2), "partial_m": round(rest),
                       "pace": _pace(rest / (T[-1] - a)), "sec": None, "hr_avg": None, "hr_max": None})
    out["splits"] = splits

    # --- Negative Split / Tempoverlauf (nach Distanz geteilt) ---
    half_d = D[-1] / 2
    t_half = time_at(half_d)
    first, second = t_half, T[-1] - t_half
    out["halves"] = {
        "first_half_pace": _pace(half_d / first),
        "second_half_pace": _pace(half_d / second),
        "diff_s_per_km": round((first - second) / (half_d / 1000), 1),  # >0 = zweite Hälfte schneller
        "negative_split": second < first,
    }
    # ohne ersten km (Einlaufen), fairerer Vergleich
    if D[-1] > 3000:
        a = time_at(1000)
        mid = 1000 + (D[-1] - 1000) / 2
        tm = time_at(mid)
        f2, s2 = tm - a, T[-1] - tm
        d2 = (D[-1] - 1000) / 2
        out["halves_without_first_km"] = {
            "first_half_pace": _pace(d2 / f2),
            "second_half_pace": _pace(d2 / s2),
            "diff_s_per_km": round((f2 - s2) / (d2 / 1000), 1),
            "negative_split": s2 < f2,
        }
    full_secs = [s["sec"] for s in splits if s.get("sec")]
    if len(full_secs) >= 3:
        mean = sum(full_secs) / len(full_secs)
        sd = (sum((x - mean) ** 2 for x in full_secs) / len(full_secs)) ** 0.5
        out["split_consistency"] = {
            "fastest_km": _fmt(min(full_secs)), "slowest_km": _fmt(max(full_secs)),
            "std_s": round(sd, 1),
        }

    # --- Puls-Drift: Tempo-pro-Puls zweite vs. erste Hälfte (ohne ersten km) ---
    hr_all = [h for h in H if h]
    if hr_all and D[-1] > 3000:
        a = time_at(1000)
        mid = 1000 + (D[-1] - 1000) / 2
        tm = time_at(mid)
        h1, h2 = hr_between(a, tm), hr_between(tm, T[-1])
        if h1 and h2:
            v1 = ((mid - 1000)) / (tm - a)
            v2 = (D[-1] - mid) / (T[-1] - tm)
            e1, e2 = v1 / (sum(h1) / len(h1)), v2 / (sum(h2) / len(h2))
            out["hr_drift"] = {
                "hr_first_half": round(sum(h1) / len(h1)),
                "hr_second_half": round(sum(h2) / len(h2)),
                "efficiency_change_pct": round((e2 / e1 - 1) * 100, 1),  # <0: bei gleichem Tempo mehr Puls
            }

    # --- Antritte ---
    sp = [(dist_at(s + W) - dist_at(s)) / W for s in range(0, max(1, N - W))]
    median = sorted(sp)[len(sp) // 2]
    thr = parse_pace(thr_pace) if thr_pace else median * rel
    out["effort_threshold_pace"] = _pace(thr)
    out["median_pace"] = _pace(median)
    runs, cur = [], None
    for s, v in enumerate(sp):
        c = s + W / 2
        if c < skip_start_s:
            continue
        if v > thr:
            cur = [c, c] if cur is None else [cur[0], c]
        elif cur:
            runs.append(cur)
            cur = None
    if cur:
        runs.append(cur)
    merged: list[list[float]] = []
    for e in runs:
        if merged and e[0] - merged[-1][1] < merge_gap_s:
            merged[-1][1] = e[1]
        else:
            merged.append(list(e))
    efforts = []
    for a, b in merged:
        a0, b0 = max(0, a - W / 2), min(N, b + W / 2)
        dt = b0 - a0
        dd = dist_at(b0) - dist_at(a0)
        if dt < 10 or dd < min_len_m:
            continue
        hh = hr_between(a0, b0 + 10)
        efforts.append({"km": round(dist_at(a0) / 1000, 2), "len_m": round(dd), "dur_s": round(dt),
                        "pace": _pace(dd / dt), "hr_max": max(hh) if hh else None})
    out["efforts"] = efforts

    out["hr"] = {"avg": round(sum(hr_all) / len(hr_all)), "max": max(hr_all)} if hr_all else None
    cad = [c for c in C if c]
    out["cadence_steps_per_min"] = round(2 * sum(cad) / len(cad)) if cad else None
    return out


def analyze_activity_json(run: dict, **kw: Any) -> dict[str, Any]:
    T, D, H, C = series_from_activity(run)
    res = analyze(T, D, H, C, **kw)
    res["points"] = len(T)
    return res


if __name__ == "__main__":
    d = json.load(open(sys.argv[1]))
    print(json.dumps(analyze_activity_json(d), indent=2, ensure_ascii=False))
