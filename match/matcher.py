"""Heuristic join: live observations -> GTFS scheduled trips.

There is NO id join available: the live feed's `spoj` number maps to nothing
in GTFS (trip_short_name is empty), and the live tripID is an internal run
instance id. So each observed run (one vehicle_id within one service date)
is matched to a GTFS trip by:

    line  +  direction (destination vs headsign/last stop)  +
    nearest scheduled time  +  position-along-route consistency

For every observation we estimate the *scheduled* clock time the vehicle
should have been at its last passed stop (observed time minus reported
delay) and compare it with each candidate trip's timetable at that stop
sequence. The trip with the smallest median discrepancy wins, if it is
within MATCH_TOLERANCE_S.

Output goes to the matched_runs table:
    (service_date, vehicle_id) -> trip_id, direction_id, poradie, score.

Run as a batch job (idempotent — reruns replace previous matches):

    python -m match.matcher --date 2026-07-01 [--line 37]
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import unicodedata
from datetime import date, datetime, timezone

import config
import storage
from gtfs.loader import gtfs_time_to_seconds
from gtfs.poradie import normalize_poradie, poradie_from_trip_id
from gtfs.service_calendar import active_service_ids

log = logging.getLogger(__name__)

# lastStopOrder's exact base (0- or 1-based) is unconfirmed; we try both
# offsets per observation and keep the better one.
STOP_ORDER_OFFSETS = (0, 1)


def normalize_text(text: str | None) -> str:
    """Lowercase, strip diacritics and punctuation — for headsign matching."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if c.isalnum() or c.isspace()).strip()


def _direction_compatible(destination: str, headsign: str, last_stop: str) -> bool:
    """Does the live destination agree with the trip's headsign (or, when the
    headsign is empty, the trip's final stop name)?"""
    dest = normalize_text(destination)
    if not dest:
        return True  # nothing to disagree with; time score must carry it
    for candidate in (normalize_text(headsign), normalize_text(last_stop)):
        if candidate and (candidate in dest or dest in candidate):
            return True
    return False


def _local_seconds(ts_utc: str, service_day: date) -> float:
    """Seconds since local midnight of the service date. May exceed 86400
    for after-midnight observations, matching GTFS >24h times."""
    dt = datetime.strptime(ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    local = dt.astimezone(config.LOCAL_TZ)
    midnight = datetime.combine(service_day, datetime.min.time(), tzinfo=config.LOCAL_TZ)
    return (local - midnight).total_seconds()


def _load_candidates(conn: sqlite3.Connection, day: date,
                     line: str) -> list[dict]:
    """All GTFS trips of `line` active on `day`, with their stop-time
    profiles: {trip_id, direction_id, headsign, last_stop_name,
    times: {stop_sequence: departure_seconds}}."""
    services = active_service_ids(conn, day)
    if not services:
        return []
    marks = ",".join("?" for _ in services)
    trips = conn.execute(
        f"""SELECT t.trip_id, t.direction_id, t.trip_headsign
            FROM gtfs_trips t
            JOIN gtfs_routes r ON r.route_id = t.route_id
            WHERE r.route_short_name = ? AND t.service_id IN ({marks})""",
        (line, *services),
    ).fetchall()

    if not trips:
        return []
    trip_marks = ",".join("?" for _ in trips)
    profiles: dict[str, list[tuple[int, int | None, str]]] = {t[0]: [] for t in trips}
    for trip_id, seq, dep, stop_name in conn.execute(
        f"""SELECT st.trip_id, CAST(st.stop_sequence AS INTEGER),
                   st.departure_time, COALESCE(s.stop_name, '')
            FROM gtfs_stop_times st
            LEFT JOIN gtfs_stops s ON s.stop_id = st.stop_id
            WHERE st.trip_id IN ({trip_marks})
            ORDER BY st.trip_id, CAST(st.stop_sequence AS INTEGER)""",
        [t[0] for t in trips],
    ):
        profiles[trip_id].append((seq, gtfs_time_to_seconds(dep), stop_name))

    candidates = []
    for trip_id, direction_id, headsign in trips:
        rows = profiles[trip_id]
        if not rows:
            continue
        times = {seq: secs for seq, secs, _name in rows if secs is not None}
        candidates.append({
            "trip_id": trip_id,
            "direction_id": direction_id,
            "headsign": headsign,
            "last_stop_name": rows[-1][2],
            "times": times,
        })
    return candidates


def _score_trip(candidate: dict, observations: list[dict], day: date) -> float | None:
    """Median |estimated scheduled time - timetable time| across the run's
    observations, in seconds. None when the trip never covers the observed
    stop sequences."""
    diffs = []
    for obs in observations:
        if obs["last_stop_order"] is None or obs["delay_minutes"] is None:
            continue
        est_scheduled = _local_seconds(obs["ts"], day) - obs["delay_minutes"] * 60
        best = None
        for offset in STOP_ORDER_OFFSETS:
            scheduled = candidate["times"].get(int(obs["last_stop_order"]) + offset)
            if scheduled is None:
                continue
            diff = abs(est_scheduled - scheduled)
            best = diff if best is None else min(best, diff)
        if best is not None:
            diffs.append(best)
    if not diffs:
        return None
    diffs.sort()
    return diffs[len(diffs) // 2]


def match_date(conn: sqlite3.Connection, day: date, line: str | None = None) -> int:
    """Match every observed run on `day` (optionally one line) and upsert
    into matched_runs. Returns the number of runs processed."""
    day_local_start = datetime.combine(day, datetime.min.time(), tzinfo=config.LOCAL_TZ)
    start_utc = day_local_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = day_local_start.replace(hour=23, minute=59, second=59).astimezone(
        timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    where = "ts >= ? AND ts <= ?"
    params: list = [start_utc, end_utc]
    if line:
        where += " AND line = ?"
        params.append(line)

    runs: dict[tuple[str, int], list[dict]] = {}
    for ts, vehicle_id, obs_line, destination, last_stop_order, delay in conn.execute(
        f"""SELECT ts, vehicle_id, line, destination, last_stop_order, delay_minutes
            FROM observations WHERE {where} ORDER BY ts""",
        params,
    ):
        if not obs_line:
            continue
        runs.setdefault((obs_line, vehicle_id), []).append({
            "ts": ts,
            "destination": destination,
            "last_stop_order": last_stop_order,
            "delay_minutes": delay,
        })

    candidates_by_line: dict[str, list[dict]] = {}
    matched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    processed = 0

    for (run_line, vehicle_id), observations in runs.items():
        if run_line not in candidates_by_line:
            candidates_by_line[run_line] = _load_candidates(conn, day, run_line)
        destination = next((o["destination"] for o in observations if o["destination"]), "")

        best_trip, best_score = None, None
        for candidate in candidates_by_line[run_line]:
            if not _direction_compatible(
                destination, candidate["headsign"], candidate["last_stop_name"]
            ):
                continue
            score = _score_trip(candidate, observations, day)
            if score is not None and (best_score is None or score < best_score):
                best_trip, best_score = candidate, score

        if best_trip is not None and best_score is not None and best_score <= config.MATCH_TOLERANCE_S:
            trip_id = best_trip["trip_id"]
            direction_id = best_trip["direction_id"]
            poradie = normalize_poradie(poradie_from_trip_id(trip_id))
        else:
            trip_id, direction_id, poradie, best_score = None, None, None, None

        conn.execute(
            """INSERT INTO matched_runs
               (service_date, vehicle_id, line, destination, trip_id,
                direction_id, poradie, score_s, n_obs, matched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (service_date, vehicle_id) DO UPDATE SET
                 line=excluded.line, destination=excluded.destination,
                 trip_id=excluded.trip_id, direction_id=excluded.direction_id,
                 poradie=excluded.poradie, score_s=excluded.score_s,
                 n_obs=excluded.n_obs, matched_at=excluded.matched_at""",
            (day.isoformat(), vehicle_id, run_line, destination, trip_id,
             direction_id, poradie, best_score, len(observations), matched_at),
        )
        processed += 1

    conn.commit()
    matched = conn.execute(
        "SELECT COUNT(*) FROM matched_runs WHERE service_date = ? AND trip_id IS NOT NULL",
        (day.isoformat(),),
    ).fetchone()[0]
    log.info("%s: %d runs processed, %d matched to GTFS trips", day, processed, matched)
    return processed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=config.DB_PATH)
    parser.add_argument("--date", required=True, help="service date YYYY-MM-DD")
    parser.add_argument("--line", default=None, help="restrict to one line, e.g. 37")
    args = parser.parse_args()

    conn = storage.connect(args.db)
    match_date(conn, date.fromisoformat(args.date), line=args.line)


if __name__ == "__main__":
    main()
