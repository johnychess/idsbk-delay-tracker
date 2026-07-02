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

import logging
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


def run_sweep(session: requests.Session,
              points: list[tuple[float, float]]) -> tuple[list[dict], dict]:
    """Query all points, dedupe by vehicle_id, return (rows, stats)."""
    started = time.monotonic()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen: dict[int, dict] = {}
    failed = 0

    for i, (lat, lng) in enumerate(points):
        try:
            for raw in fetch_point(session, lat, lng):
                row = parse_vehicle(raw, ts)
                if row is not None and row["vehicle_id"] not in seen:
                    seen[row["vehicle_id"]] = row
        except Exception as exc:  # any single point failing must not kill the sweep
            failed += 1
            log.warning("point (%s, %s) failed: %s", lat, lng, exc)
        if i < len(points) - 1:
            time.sleep(config.INTER_POINT_DELAY_S)

    stats = {
        "ts": ts,
        "points_queried": len(points),
        "points_failed": failed,
        "vehicles_seen": len(seen),
        "duration_s": round(time.monotonic() - started, 2),
    }
    return list(seen.values()), stats
