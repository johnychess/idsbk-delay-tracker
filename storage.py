"""SQLite storage layer shared by the collector, the GTFS loader and the
analyser.

Design rules:
- append-only for observations (raw log is sacred; filtering happens at
  analysis time),
- WAL mode so the analyser can read while the collector writes,
- GTFS tables are fully replaced on each refresh (they mirror the feed).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id              INTEGER PRIMARY KEY,
    ts              TEXT NOT NULL,      -- sweep start, UTC ISO-8601 ...Z
    vehicle_id      INTEGER NOT NULL,   -- == live tripID: a run instance, NOT a physical bus
    line            TEXT,
    spoj            INTEGER,            -- live "trip" number; does NOT id-join to GTFS
    destination     TEXT,
    vehicle_type    TEXT,               -- BUS / TROLLEY / TRAM / URBAN / TRAIN
    is_urban        INTEGER,
    operator        TEXT,
    lat             REAL,
    lng             REAL,
    last_stop_order INTEGER,
    is_on_stop      INTEGER,
    delay_minutes   INTEGER,
    license_number  TEXT                -- almost always NULL in the feed
);
CREATE INDEX IF NOT EXISTS idx_obs_line_ts ON observations (line, ts);
CREATE INDEX IF NOT EXISTS idx_obs_vehicle_ts ON observations (vehicle_id, ts);

-- One row per sweep: lets the analyser distinguish "no vehicle appeared"
-- (a genuinely missed departure) from "the collector was down".
CREATE TABLE IF NOT EXISTS sweeps (
    ts              TEXT NOT NULL,      -- sweep start, UTC ISO-8601
    points_queried  INTEGER,
    points_failed   INTEGER,
    vehicles_seen   INTEGER,
    duration_s      REAL,
    max_point_count INTEGER,            -- most vehicles any single point returned
    points_at_cap   INTEGER,            -- points that hit the 100-vehicle cap (saturated)
    point_counts    TEXT                -- JSON array of per-point raw counts (null = failed)
);
CREATE INDEX IF NOT EXISTS idx_sweeps_ts ON sweeps (ts);

-- Daily vehicle <-> poradie assignment parsed from imhd.sk/ba/vyprava.
CREATE TABLE IF NOT EXISTS vyprava (
    date        TEXT NOT NULL,          -- local service date YYYY-MM-DD
    line        TEXT NOT NULL,
    poradie     TEXT NOT NULL,          -- "1", "2a", "51", ...
    vehicle     TEXT NOT NULL,          -- evidenčné číslo, e.g. "3319"
    fetched_at  TEXT NOT NULL,
    confirmed   INTEGER,                -- 1 = imhd-verified, 0 = provisional
    UNIQUE (date, line, poradie, vehicle)
);

-- Result of the heuristic live->GTFS join (match/matcher.py).
CREATE TABLE IF NOT EXISTS matched_runs (
    service_date  TEXT NOT NULL,        -- local date YYYY-MM-DD
    vehicle_id    INTEGER NOT NULL,     -- live run instance
    line          TEXT,
    destination   TEXT,
    trip_id       TEXT,                 -- GTFS trip_id (NULL = no match within tolerance)
    direction_id  TEXT,
    poradie       TEXT,                 -- decoded from trip_id, zero-padding stripped
    score_s       REAL,                 -- median schedule discrepancy of the winning trip
    n_obs         INTEGER,
    matched_at    TEXT NOT NULL,
    UNIQUE (service_date, vehicle_id)
);

-- One row per distinct GTFS feed we have ever downloaded, identified by a
-- hash of the zip. Feeds are ARCHIVED, never overwritten: a feed only
-- declares service for its own validity window, so replacing it would make
-- every earlier observation unmatchable (it did — see feed_for_date()).
CREATE TABLE IF NOT EXISTS gtfs_feeds (
    feed_id      TEXT PRIMARY KEY,      -- sha256 prefix of the zip bytes
    downloaded_at TEXT NOT NULL,        -- when first seen
    last_seen_at TEXT NOT NULL,         -- when last re-downloaded unchanged
    start_date   TEXT,                  -- earliest calendar.start_date (YYYYMMDD)
    end_date     TEXT,                  -- latest calendar.end_date
    source_url   TEXT,
    n_trips      INTEGER
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

OBS_COLUMNS = [
    "ts", "vehicle_id", "line", "spoj", "destination", "vehicle_type",
    "is_urban", "operator", "lat", "lng", "last_stop_order", "is_on_stop",
    "delay_minutes", "license_number",
]


def connect(db_path: str) -> sqlite3.Connection:
    directory = os.path.dirname(db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for DBs created before a column existed. SQLite
    can't ALTER ... ADD COLUMN IF NOT EXISTS, so check pragma first."""
    sweep_cols = {row[1] for row in conn.execute("PRAGMA table_info(sweeps)")}
    for col, decl in (("max_point_count", "INTEGER"),
                      ("points_at_cap", "INTEGER"),
                      ("point_counts", "TEXT")):
        if col not in sweep_cols:
            conn.execute(f"ALTER TABLE sweeps ADD COLUMN {col} {decl}")
    vyprava_cols = {row[1] for row in conn.execute("PRAGMA table_info(vyprava)")}
    if "confirmed" not in vyprava_cols:
        conn.execute("ALTER TABLE vyprava ADD COLUMN confirmed INTEGER")
    conn.commit()
    _migrate_gtfs_feed_id(conn)


GTFS_TABLES = ("gtfs_routes", "gtfs_trips", "gtfs_stops", "gtfs_stop_times",
               "gtfs_calendar", "gtfs_calendar_dates")


def _migrate_gtfs_feed_id(conn: sqlite3.Connection) -> None:
    """Tag pre-archiving GTFS rows with a synthetic feed so they stay usable.

    Before archiving existed the gtfs_* tables held exactly one feed, replaced
    on every refresh. Give those rows a feed_id and register the feed with the
    validity window read from its own calendar."""
    existing = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'gtfs_%'")}
    legacy = [t for t in GTFS_TABLES if t in existing]
    if not legacy:
        return

    tagged_any = False
    for table in legacy:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "feed_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN feed_id TEXT")
            tagged_any = True
    if not tagged_any:
        return

    untagged = conn.execute(
        "SELECT COUNT(*) FROM gtfs_trips WHERE feed_id IS NULL").fetchone()[0]
    if not untagged:
        return

    feed_id = "legacy"
    for table in legacy:
        conn.execute(f"UPDATE {table} SET feed_id = ? WHERE feed_id IS NULL",
                     (feed_id,))
    window = conn.execute(
        "SELECT MIN(start_date), MAX(end_date) FROM gtfs_calendar").fetchone()
    downloaded = get_meta(conn, "gtfs_downloaded_at") or "unknown"
    conn.execute(
        "INSERT OR REPLACE INTO gtfs_feeds"
        " (feed_id, downloaded_at, last_seen_at, start_date, end_date,"
        "  source_url, n_trips) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (feed_id, downloaded, downloaded, window[0], window[1],
         get_meta(conn, "gtfs_source_url"), untagged),
    )
    conn.commit()


def insert_observations(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    placeholders = ", ".join(f":{c}" for c in OBS_COLUMNS)
    cur = conn.executemany(
        f"INSERT INTO observations ({', '.join(OBS_COLUMNS)}) VALUES ({placeholders})",
        list(rows),
    )
    conn.commit()
    return cur.rowcount


def record_sweep(conn: sqlite3.Connection, ts: str, points_queried: int,
                 points_failed: int, vehicles_seen: int, duration_s: float,
                 max_point_count: int | None = None,
                 points_at_cap: int | None = None,
                 point_counts: str | None = None) -> None:
    conn.execute(
        "INSERT INTO sweeps (ts, points_queried, points_failed, vehicles_seen,"
        " duration_s, max_point_count, points_at_cap, point_counts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, points_queried, points_failed, vehicles_seen, duration_s,
         max_point_count, points_at_cap, point_counts),
    )
    conn.commit()


def replace_vyprava(conn: sqlite3.Connection, date: str,
                    entries: Iterable[tuple[str, str, str]], fetched_at: str,
                    confirmed: bool) -> int:
    """Replace ALL rows for `date` with these entries, so a confirmed (or
    corrected) roster overwrites an earlier provisional fetch. entries:
    iterable of (line, poradie, vehicle)."""
    conn.execute("DELETE FROM vyprava WHERE date = ?", (date,))
    cur = conn.executemany(
        "INSERT OR IGNORE INTO vyprava"
        " (date, line, poradie, vehicle, fetched_at, confirmed)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(date, line, poradie, vehicle, fetched_at, int(confirmed))
         for line, poradie, vehicle in entries],
    )
    conn.commit()
    return cur.rowcount


def gtfs_feed_exists(conn: sqlite3.Connection, feed_id: str) -> bool:
    return conn.execute("SELECT 1 FROM gtfs_feeds WHERE feed_id = ?",
                        (feed_id,)).fetchone() is not None


def register_gtfs_feed(conn: sqlite3.Connection, feed_id: str, downloaded_at: str,
                       start_date: str | None, end_date: str | None,
                       source_url: str, n_trips: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO gtfs_feeds (feed_id, downloaded_at, last_seen_at,"
        " start_date, end_date, source_url, n_trips) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (feed_id, downloaded_at, downloaded_at, start_date, end_date,
         source_url, n_trips),
    )
    conn.commit()


def touch_gtfs_feed(conn: sqlite3.Connection, feed_id: str, seen_at: str) -> None:
    conn.execute("UPDATE gtfs_feeds SET last_seen_at = ? WHERE feed_id = ?",
                 (seen_at, feed_id))
    conn.commit()


def feed_for_date(conn: sqlite3.Connection, day) -> str | None:
    """The archived feed that was in effect on `day`.

    Picks the feed whose validity window covers the date; when several do,
    the one with the latest start_date (the most recent applicable revision).
    Returns None when no archived feed covers the date — callers must treat
    that as "schedule unknown", NOT as "nothing was scheduled"."""
    ymd = day.strftime("%Y%m%d")
    row = conn.execute(
        "SELECT feed_id FROM gtfs_feeds"
        " WHERE start_date <= ? AND end_date >= ?"
        " ORDER BY start_date DESC, downloaded_at DESC LIMIT 1",
        (ymd, ymd),
    ).fetchone()
    return row[0] if row else None


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
