"""Central configuration for the IDS BK delay tracker.

Every value can be overridden via an environment variable (see .env.example).
Keep this module dependency-free so both the collector and the analyser can
import it cheaply.
"""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# General
# --------------------------------------------------------------------------

LOCAL_TZ = ZoneInfo(os.environ.get("TRACKER_TZ", "Europe/Bratislava"))

# SQLite file. On Railway this MUST live on the attached Volume
# (e.g. /data/tracker.sqlite) — the container filesystem is wiped on redeploy.
DB_PATH = os.environ.get("DB_PATH", "data/tracker.sqlite")

USER_AGENT = os.environ.get(
    "USER_AGENT",
    "idsbk-delay-tracker/0.1 (personal punctuality research; contact via repo)",
)

REQUEST_TIMEOUT_S = float(os.environ.get("REQUEST_TIMEOUT_S", "15"))

# --------------------------------------------------------------------------
# Live vehicle feed (mapa.idsbk.sk)
# --------------------------------------------------------------------------

VEHICLES_URL = os.environ.get(
    "VEHICLES_URL", "https://mapa.idsbk.sk/navigation/vehicles/nearby"
)

# The endpoint ignores `radius` and always returns the 100 vehicles nearest
# to the query point, so we tile the area with multiple points and dedupe.
# Vehicles report roughly every 2 minutes; polling faster only duplicates.
SWEEP_INTERVAL_S = int(os.environ.get("SWEEP_INTERVAL_S", "120"))

# Pause between individual point queries inside one sweep (politeness).
INTER_POINT_DELAY_S = float(os.environ.get("INTER_POINT_DELAY_S", "1.0"))

# Optional overnight pause (local time, "HH:MM"). Set both empty to disable.
PAUSE_FROM = os.environ.get("PAUSE_FROM", "00:30")
PAUSE_TO = os.environ.get("PAUSE_TO", "04:30")

# Bratislava bounding box (whole network).
BBOX = (48.10, 48.24, 16.99, 17.21)  # lat_min, lat_max, lng_min, lng_max


def _grid_points(lat_min: float, lat_max: float, lng_min: float, lng_max: float,
                 rows: int, cols: int) -> list[tuple[float, float]]:
    """Evenly spread points, inset by half a cell so points sit in cell centres."""
    dlat = (lat_max - lat_min) / rows
    dlng = (lng_max - lng_min) / cols
    return [
        (round(lat_min + dlat * (r + 0.5), 5), round(lng_min + dlng * (c + 0.5), 5))
        for r in range(rows)
        for c in range(cols)
    ]


# A 5x5 grid (~25 points) captures most of the ~350-370 active vehicles per
# sweep. Denser than 4x4 on purpose: because `radius` is ignored and each
# point returns only its 100 nearest vehicles, sparse points let the dense
# city centre "starve" the edges — the Záhorská Bystrica terminus (line 37's
# origin) sat ~1.9 km from the nearest 4x4 point and its departures were
# intermittently missed, inflating the missed-departure count. 5x5 puts a
# point within ~0.8 km of that terminus. Still well under the 120 s budget.
NETWORK_GRID = (int(os.environ.get("GRID_ROWS", "5")),
                int(os.environ.get("GRID_COLS", "5")))
NETWORK_POINTS = _grid_points(*BBOX, rows=NETWORK_GRID[0], cols=NETWORK_GRID[1])

# Line-37 corridor (Záhorská Bystrica -> centre / Most SNP) for narrow focus.
LINE37_POINTS = [
    (48.2235, 17.0445),
    (48.2100, 17.0450),
    (48.1900, 17.0600),
    (48.1665, 17.0779),
    (48.1500, 17.0750),
    (48.1405, 17.1045),
    (48.1580, 17.1070),
]


def _points_from_env(raw: str) -> list[tuple[float, float]]:
    points = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        lat, lng = chunk.split(",")
        points.append((float(lat), float(lng)))
    return points


# COVERAGE=network (default, all lines — costs the same to run) or line37.
# SWEEP_POINTS="lat,lng;lat,lng;..." overrides both.
_coverage = os.environ.get("COVERAGE", "network").strip().lower()
if os.environ.get("SWEEP_POINTS"):
    SWEEP_POINTS = _points_from_env(os.environ["SWEEP_POINTS"])
elif _coverage == "line37":
    SWEEP_POINTS = LINE37_POINTS
else:
    SWEEP_POINTS = NETWORK_POINTS

# --------------------------------------------------------------------------
# GTFS static schedule (City of Bratislava, CC-BY 4.0 — attribute!)
# --------------------------------------------------------------------------

# Current feed pointer; the stable dataset landing page is on
# data.bratislava.sk (ArcGIS Hub). Re-check freshness weekly.
GTFS_URL = os.environ.get(
    "GTFS_URL",
    "https://www.arcgis.com/sharing/rest/content/items/"
    "aba12fd2cbac4843bc7406151bc66106/data",
)
GTFS_REFRESH_DAYS = int(os.environ.get("GTFS_REFRESH_DAYS", "7"))

# --------------------------------------------------------------------------
# imhd.sk daily "výprava" (vehicle <-> poradie assignment)
# --------------------------------------------------------------------------

# Public server-rendered page; ?d=YYYY-MM-DD works for historical days.
# Never touch imhd's login-gated /rt/ realtime endpoints.
VYPRAVA_URL = os.environ.get("VYPRAVA_URL", "https://imhd.sk/ba/vyprava")

# Run the výprava pass once per day after this local hour (assignments
# accumulate during the day; late fetch = most complete table).
VYPRAVA_FETCH_HOUR = int(os.environ.get("VYPRAVA_FETCH_HOUR", "20"))

# How many days back to keep re-checking for confirmation. imhd verifies a
# day ~2 days later (the "not yet verified" note disappears); until then we
# re-fetch it each day and overwrite with the verified roster once it lands.
VYPRAVA_LOOKBACK_DAYS = int(os.environ.get("VYPRAVA_LOOKBACK_DAYS", "5"))

# A day is never trusted as "confirmed" until it is at least this many days
# old, even if the "not yet verified" note is absent. Guards against imhd
# rewording the note (which would otherwise make every page read as verified
# and lock same-day provisional data as final).
VYPRAVA_MIN_CONFIRM_AGE_DAYS = int(os.environ.get("VYPRAVA_MIN_CONFIRM_AGE_DAYS", "2"))

# --------------------------------------------------------------------------
# Matching / analysis defaults
# --------------------------------------------------------------------------

# Reject a live->GTFS trip match when the median schedule-time discrepancy
# exceeds this many seconds.
MATCH_TOLERANCE_S = int(os.environ.get("MATCH_TOLERANCE_S", "600"))

# Analysis-time filters (the raw log is never mutated).
MAX_PLAUSIBLE_DELAY_MIN = int(os.environ.get("MAX_PLAUSIBLE_DELAY_MIN", "90"))
MIN_PLAUSIBLE_DELAY_MIN = int(os.environ.get("MIN_PLAUSIBLE_DELAY_MIN", "-15"))

# "On time" threshold for punctuality percentages.
ON_TIME_THRESHOLD_MIN = int(os.environ.get("ON_TIME_THRESHOLD_MIN", "2"))

# Missed-departure detection window around the scheduled origin departure.
MISSED_WINDOW_BEFORE_MIN = int(os.environ.get("MISSED_WINDOW_BEFORE_MIN", "5"))
MISSED_WINDOW_AFTER_MIN = int(os.environ.get("MISSED_WINDOW_AFTER_MIN", "25"))
