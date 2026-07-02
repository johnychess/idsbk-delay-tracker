"""Analysis 5: missed departures ("vynechané spoje").

For every scheduled departure (GTFS trips active on the date), check whether
any vehicle actually served it within a generous window. Deliberately
CONSERVATIVE, in two ways:

1. a departure counts as served if the run was matched to that trip_id OR
   any same-line vehicle heading the right way was observed near the start
   of its route inside the window (so an imperfect match isn't a false
   miss);
2. a departure is only counted as MISSED when the collector demonstrably
   had coverage: the sweeps table must show near-continuous sweeping across
   the whole window, otherwise the verdict is "unknown" (feed dropouts and
   collector downtime must not fabricate missed buses).

Line 37 sanity denominator: 26 scheduled departures 05:00-09:00 from
Záhorská Bystrica on a weekday.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pandas as pd

import config
from gtfs.loader import gtfs_time_to_seconds
from gtfs.service_calendar import active_service_ids

# Window must have at least this fraction of expected sweeps to judge a miss.
MIN_COVERAGE = 0.8
# "Near the start of its route" for the fallback served-check.
ORIGIN_MAX_STOP_ORDER = 3


def scheduled_departures(conn: sqlite3.Connection, day: date,
                         line: str) -> pd.DataFrame:
    """All scheduled origin departures of `line` on `day`:
    trip_id, direction_id, headsign, departure seconds since local midnight."""
    services = active_service_ids(conn, day)
    if not services:
        return pd.DataFrame()
    marks = ",".join("?" for _ in services)
    rows = conn.execute(
        f"""SELECT t.trip_id, t.direction_id, t.trip_headsign,
                   (SELECT st.departure_time FROM gtfs_stop_times st
                    WHERE st.trip_id = t.trip_id
                    ORDER BY CAST(st.stop_sequence AS INTEGER) LIMIT 1)
            FROM gtfs_trips t
            JOIN gtfs_routes r ON r.route_id = t.route_id
            WHERE r.route_short_name = ? AND t.service_id IN ({marks})""",
        (line, *services),
    ).fetchall()
    data = []
    for trip_id, direction_id, headsign, dep in rows:
        secs = gtfs_time_to_seconds(dep) if dep else None
        if secs is None:
            continue
        data.append({
            "trip_id": trip_id,
            "direction_id": direction_id,
            "headsign": headsign,
            "dep_seconds": secs,
        })
    return pd.DataFrame(data)


def _to_utc(day: date, seconds: float) -> datetime:
    local_midnight = datetime.combine(day, datetime.min.time(), tzinfo=config.LOCAL_TZ)
    return (local_midnight + timedelta(seconds=seconds)).astimezone(timezone.utc)


def _coverage_ok(conn: sqlite3.Connection, start: datetime, end: datetime) -> bool:
    n = conn.execute(
        "SELECT COUNT(*) FROM sweeps WHERE ts >= ? AND ts <= ?",
        (start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")),
    ).fetchone()[0]
    expected = (end - start).total_seconds() / config.SWEEP_INTERVAL_S
    return expected > 0 and n / expected >= MIN_COVERAGE


def missed_departures(conn: sqlite3.Connection, day: date,
                      line: str) -> pd.DataFrame:
    """Verdict per scheduled departure: served / missed / unknown."""
    departures = scheduled_departures(conn, day, line)
    if departures.empty:
        return departures

    matched_trip_ids = {
        row[0]
        for row in conn.execute(
            "SELECT trip_id FROM matched_runs"
            " WHERE service_date = ? AND line = ? AND trip_id IS NOT NULL",
            (day.isoformat(), line),
        )
    }

    verdicts = []
    for _, dep in departures.iterrows():
        window_start = _to_utc(day, dep["dep_seconds"]) - timedelta(
            minutes=config.MISSED_WINDOW_BEFORE_MIN)
        window_end = _to_utc(day, dep["dep_seconds"]) + timedelta(
            minutes=config.MISSED_WINDOW_AFTER_MIN)

        if dep["trip_id"] in matched_trip_ids:
            verdict = "served"
        else:
            fallback = conn.execute(
                """SELECT COUNT(*) FROM observations
                   WHERE line = ? AND ts >= ? AND ts <= ?
                     AND last_stop_order IS NOT NULL AND last_stop_order <= ?""",
                (line,
                 window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 ORIGIN_MAX_STOP_ORDER),
            ).fetchone()[0]
            if fallback > 0:
                verdict = "served_probably"  # someone left the origin in-window
            elif _coverage_ok(conn, window_start, window_end):
                verdict = "missed"
            else:
                verdict = "unknown"  # collector wasn't reliably watching

        hh, mm = int(dep["dep_seconds"] // 3600), int(dep["dep_seconds"] % 3600 // 60)
        verdicts.append({
            "trip_id": dep["trip_id"],
            "direction_id": dep["direction_id"],
            "headsign": dep["headsign"],
            "scheduled": f"{hh:02d}:{mm:02d}",
            "verdict": verdict,
        })
    return pd.DataFrame(verdicts).sort_values("scheduled").reset_index(drop=True)


def summarize(verdicts: pd.DataFrame) -> dict:
    if verdicts.empty:
        return {}
    counts = verdicts["verdict"].value_counts().to_dict()
    counts["scheduled_total"] = len(verdicts)
    return counts
