"""One sweep = query every tiling point once, dedupe, return observation rows.

Endpoint quirks this module is built around (verified empirically):
- `radius` is ignored: every call returns the 100 vehicles nearest to the
  point (hard cap), hence the tiling + dedupe-by-vehicleID design.
- `vehicleID` equals `tripID` — it identifies a scheduled run instance,
  not a physical bus. `licenseNumber` is almost always null.
- Vehicles report roughly every 2 minutes, so sweeping faster than
  ~120 s only yields duplicate rows.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timezone

import requests

import config

log = logging.getLogger(__name__)

HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://mapa.idsbk.sk/",
    "User-Agent": config.USER_AGENT,
}


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def fetch_point(session: requests.Session, lat: float, lng: float) -> list[dict]:
    """Return the raw vehicle objects near one query point."""
    resp = session.get(
        config.VEHICLES_URL,
        params={"lat": lat, "lng": lng, "radius": 5000},
        timeout=config.REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    payload = resp.json()
    vehicles = payload.get("vehicles")
    if not isinstance(vehicles, list):
        raise ValueError(f"unexpected payload shape: keys={list(payload)[:10]}")
    return vehicles


def parse_vehicle(raw: dict, ts: str) -> dict | None:
    """Flatten one vehicle object into an observations row. Tolerant of
    missing sub-objects; returns None when there is no usable identity."""
    vehicle_id = raw.get("vehicleID")
    if vehicle_id is None:
        return None
    tt = raw.get("timeTableTrip") or {}
    line_info = tt.get("timeTableLine") or {}
    return {
        "ts": ts,
        "vehicle_id": vehicle_id,
        "line": line_info.get("line"),
        "spoj": tt.get("trip"),
        "destination": tt.get("destination"),
        "vehicle_type": line_info.get("ezVehicleType"),
        "is_urban": _as_int(line_info.get("ezIsUrban")),
        "operator": line_info.get("operatorName"),
        "lat": raw.get("latitude"),
        "lng": raw.get("longitude"),
        "last_stop_order": raw.get("lastStopOrder"),
        "is_on_stop": _as_int(raw.get("isOnStop")),
        "delay_minutes": raw.get("delayMinutes"),
        "license_number": raw.get("licenseNumber"),
    }


def _as_int(value) -> int | None:
    if value is None:
        return None
    return int(bool(value))


# The endpoint returns at most this many vehicles per point (radius ignored).
VEHICLE_CAP = 100


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_neighbour_km(points: list[tuple[float, float]]) -> list[float]:
    """Distance from each point to its closest sibling. A point only needs to
    reach half of this to cover the territory between them."""
    out = []
    for i, (lat, lng) in enumerate(points):
        others = [haversine_km(lat, lng, o_lat, o_lng)
                  for j, (o_lat, o_lng) in enumerate(points) if j != i]
        out.append(min(others) if others else float("inf"))
    return out


def point_reach_km(lat: float, lng: float, vehicles: list[dict]) -> float | None:
    """How far this point had to look to fill its result — the distance to the
    farthest vehicle it returned. None when it returned nothing."""
    far = None
    for raw in vehicles:
        v_lat, v_lng = raw.get("latitude"), raw.get("longitude")
        if v_lat is None or v_lng is None:
            continue
        d = haversine_km(lat, lng, v_lat, v_lng)
        if far is None or d > far:
            far = d
    return far


def summarize_points(point_counts: list[int | None],
                     reaches: list[float | None] | None = None,
                     nn_km: list[float] | None = None) -> dict:
    """Coverage diagnosis for one sweep.

    `points_at_cap` alone is NOT a coverage signal: with a few hundred
    vehicles in a compact city, essentially every point returns its full 100
    at any hour, so that counter tracks fleet size rather than data loss.

    The signal that matters is REACH. A point that hits the cap has only
    truncated something we care about if it also failed to see as far as the
    midpoint to its nearest neighbouring point — i.e. it could not cover its
    own cell, so vehicles in the gap between points were missed by both.
    `points_undercovering` counts exactly that; when it is 0 the grid is
    provably dense enough, however many points sat at the cap."""
    valid = [c for c in point_counts if c is not None]
    max_count = max(valid) if valid else 0
    at_cap = sum(1 for c in valid if c >= VEHICLE_CAP)

    undercovering = 0
    min_margin = None
    if reaches is not None and nn_km is not None:
        for count, reach, nn in zip(point_counts, reaches, nn_km):
            if count is None or reach is None:
                continue
            required = nn / 2.0
            margin = reach - required
            if min_margin is None or margin < min_margin:
                min_margin = margin
            if count >= VEHICLE_CAP and margin < 0:
                undercovering += 1

    return {
        "max_point_count": max_count,
        "points_at_cap": at_cap,
        "points_undercovering": undercovering,
        "min_reach_margin_km": round(min_margin, 3) if min_margin is not None else None,
    }


def run_sweep(session: requests.Session,
              points: list[tuple[float, float]]) -> tuple[list[dict], dict]:
    """Query all points, dedupe by vehicle_id, return (rows, stats)."""
    started = time.monotonic()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen: dict[int, dict] = {}
    failed = 0
    point_counts: list[int | None] = []  # raw count per point (None = failed)
    reaches: list[float | None] = []     # km to the farthest vehicle each returned

    for i, (lat, lng) in enumerate(points):
        try:
            vehicles = fetch_point(session, lat, lng)
            point_counts.append(len(vehicles))
            reaches.append(point_reach_km(lat, lng, vehicles))
            for raw in vehicles:
                row = parse_vehicle(raw, ts)
                if row is not None and row["vehicle_id"] not in seen:
                    seen[row["vehicle_id"]] = row
        except Exception as exc:  # any single point failing must not kill the sweep
            failed += 1
            point_counts.append(None)
            reaches.append(None)
            log.warning("point (%s, %s) failed: %s", lat, lng, exc)
        if i < len(points) - 1:
            time.sleep(config.INTER_POINT_DELAY_S)

    coverage = summarize_points(point_counts, reaches, nearest_neighbour_km(points))
    if coverage["points_undercovering"]:
        log.warning(
            "%d/%d points were capped AND could not reach their own cell — the "
            "grid is genuinely too sparse there; raise GRID_ROWS/GRID_COLS",
            coverage["points_undercovering"], len(points),
        )

    stats = {
        "ts": ts,
        "points_queried": len(points),
        "points_failed": failed,
        "vehicles_seen": len(seen),
        "duration_s": round(time.monotonic() - started, 2),
        "max_point_count": coverage["max_point_count"],
        "points_at_cap": coverage["points_at_cap"],
        "points_undercovering": coverage["points_undercovering"],
        "min_reach_margin_km": coverage["min_reach_margin_km"],
        "point_counts": json.dumps(point_counts),
        "point_reach_km": json.dumps(
            [round(r, 3) if r is not None else None for r in reaches]),
    }
    return list(seen.values()), stats
