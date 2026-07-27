"""Download the Bratislava GTFS feed (zip) and load the tables the tracker
needs into SQLite (gtfs_* tables, fully replaced on each refresh).

The feed is published by the City of Bratislava under CC-BY 4.0 — outputs
built on it must attribute the city. The download URL is a feed *pointer*
that may move; it is configurable (GTFS_URL) and refreshed weekly.

Schedule facts the rest of the code relies on:
- routes.txt: line 37 -> route_id 37012 (route_short_name "37"),
- trips.txt: trip_short_name and block_id are EMPTY; the run number lives
  in trip_id (see gtfs/poradie.py),
- stop_times departure_time can exceed 24:00:00 for after-midnight trips.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import sqlite3
import zipfile
from datetime import datetime

import requests

import config
import storage

log = logging.getLogger(__name__)

# table -> (filename, columns to keep)
TABLES: dict[str, tuple[str, list[str]]] = {
    "gtfs_routes": ("routes.txt", ["route_id", "route_short_name", "route_long_name", "route_type"]),
    "gtfs_trips": ("trips.txt", ["trip_id", "route_id", "service_id", "trip_headsign", "direction_id"]),
    "gtfs_stops": ("stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon"]),
    "gtfs_stop_times": ("stop_times.txt", ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"]),
    "gtfs_calendar": ("calendar.txt", ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "start_date", "end_date"]),
    "gtfs_calendar_dates": ("calendar_dates.txt", ["service_id", "date", "exception_type"]),
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_gtfs_trips_route ON gtfs_trips (route_id)",
    "CREATE INDEX IF NOT EXISTS idx_gtfs_st_trip ON gtfs_stop_times (trip_id, stop_sequence)",
    "CREATE INDEX IF NOT EXISTS idx_gtfs_routes_short ON gtfs_routes (route_short_name)",
]


def download_feed(url: str = "") -> bytes:
    url = url or config.GTFS_URL
    log.info("downloading GTFS feed from %s", url)
    resp = requests.get(
        url,
        headers={"User-Agent": config.USER_AGENT},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.content


def feed_fingerprint(feed_bytes: bytes) -> str:
    """Stable id for a feed's contents, so an unchanged weekly re-download is
    recognised and not archived twice."""
    return hashlib.sha256(feed_bytes).hexdigest()[:16]


def load_zip(conn: sqlite3.Connection, feed_bytes: bytes, feed_id: str) -> None:
    """Load a feed's tables, tagging every row with feed_id. Existing rows for
    other feeds are left alone — feeds accumulate, they don't replace."""
    with zipfile.ZipFile(io.BytesIO(feed_bytes)) as zf:
        names = set(zf.namelist())
        for table, (filename, columns) in TABLES.items():
            _ensure_table(conn, table, columns)
            conn.execute(f"DELETE FROM {table} WHERE feed_id = ?", (feed_id,))
            if filename not in names:
                # calendar.txt or calendar_dates.txt may legitimately be absent
                log.warning("%s missing from feed; leaving %s empty", filename, table)
                continue
            with zf.open(filename) as fh:
                _load_csv(conn, table, columns, feed_id,
                          io.TextIOWrapper(fh, encoding="utf-8-sig"))
    for stmt in INDEXES:
        conn.execute(stmt)
    conn.commit()


def _ensure_table(conn: sqlite3.Connection, table: str, columns: list[str]) -> None:
    cols = ", ".join(f"{c} TEXT" for c in columns)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({cols}, feed_id TEXT)")
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if "feed_id" not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN feed_id TEXT")


def _load_csv(conn: sqlite3.Connection, table: str, columns: list[str],
              feed_id: str, fh) -> None:
    reader = csv.DictReader(fh)
    placeholders = ", ".join("?" for _ in columns)
    insert = (f"INSERT INTO {table} ({', '.join(columns)}, feed_id)"
              f" VALUES ({placeholders}, ?)")
    batch: list[tuple] = []
    total = 0
    for row in reader:
        batch.append(tuple((row.get(c) or "").strip() for c in columns) + (feed_id,))
        if len(batch) >= 5000:
            conn.executemany(insert, batch)
            total += len(batch)
            batch.clear()
    if batch:
        conn.executemany(insert, batch)
        total += len(batch)
    log.info("loaded %s: %d rows", table, total)


def refresh(conn: sqlite3.Connection, url: str = "") -> str:
    """Download the feed and ARCHIVE it (never overwrite earlier feeds).

    A feed only declares service for its own validity window, so replacing the
    tables on every refresh silently orphaned all previously collected dates.
    Feeds are now keyed by a content hash: an unchanged re-download is a no-op,
    a new revision is added alongside the old ones, and each observation date
    is matched against the feed that was valid then (storage.feed_for_date).

    Returns the feed_id now in effect."""
    feed = download_feed(url)
    feed_id = feed_fingerprint(feed)
    now = datetime.now(config.LOCAL_TZ).isoformat()

    if storage.gtfs_feed_exists(conn, feed_id):
        storage.touch_gtfs_feed(conn, feed_id, now)
        log.info("GTFS feed %s unchanged; archive already has it", feed_id)
    else:
        load_zip(conn, feed, feed_id)
        window = conn.execute(
            "SELECT MIN(start_date), MAX(end_date) FROM gtfs_calendar"
            " WHERE feed_id = ?", (feed_id,)).fetchone()
        n_trips = conn.execute(
            "SELECT COUNT(*) FROM gtfs_trips WHERE feed_id = ?",
            (feed_id,)).fetchone()[0]
        storage.register_gtfs_feed(conn, feed_id, now, window[0], window[1],
                                   url or config.GTFS_URL, n_trips)
        log.info("GTFS feed %s archived: %s..%s, %d trips",
                 feed_id, window[0], window[1], n_trips)

    storage.set_meta(conn, "gtfs_downloaded_at", now)
    storage.set_meta(conn, "gtfs_source_url", url or config.GTFS_URL)
    return feed_id


def gtfs_time_to_seconds(hms: str) -> int | None:
    """'26:15:00' -> 94500. GTFS times can exceed 24h for after-midnight
    trips that belong to the previous service date."""
    try:
        h, m, s = hms.split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except (ValueError, AttributeError):
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    connection = storage.connect(config.DB_PATH)
    refresh(connection)
