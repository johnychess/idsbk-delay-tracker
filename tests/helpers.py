"""Shared fixtures: a synthetic GTFS schedule for line 37 in SQLite."""

from __future__ import annotations

import sqlite3

import storage


def make_db(path: str) -> sqlite3.Connection:
    conn = storage.connect(path)
    conn.execute("CREATE TABLE gtfs_routes (route_id TEXT, route_short_name TEXT,"
                 " route_long_name TEXT, route_type TEXT)")
    conn.execute("CREATE TABLE gtfs_trips (trip_id TEXT, route_id TEXT,"
                 " service_id TEXT, trip_headsign TEXT, direction_id TEXT)")
    conn.execute("CREATE TABLE gtfs_stops (stop_id TEXT, stop_name TEXT,"
                 " stop_lat TEXT, stop_lon TEXT)")
    conn.execute("CREATE TABLE gtfs_stop_times (trip_id TEXT, arrival_time TEXT,"
                 " departure_time TEXT, stop_id TEXT, stop_sequence TEXT)")
    conn.execute("CREATE TABLE gtfs_calendar (service_id TEXT, monday TEXT,"
                 " tuesday TEXT, wednesday TEXT, thursday TEXT, friday TEXT,"
                 " saturday TEXT, sunday TEXT, start_date TEXT, end_date TEXT)")
    conn.execute("CREATE TABLE gtfs_calendar_dates (service_id TEXT, date TEXT,"
                 " exception_type TEXT)")

    conn.execute("INSERT INTO gtfs_routes VALUES ('37012', '37', 'ZB - Most SNP', '3')")
    conn.execute("INSERT INTO gtfs_calendar VALUES"
                 " ('wd','1','1','1','1','1','0','0','20260101','20261231')")
    for i in range(1, 6):
        conn.execute("INSERT INTO gtfs_stops VALUES (?, ?, ?, ?)",
                     (f"S{i}", f"Stop {i}", "48.2", "17.05"))
    conn.commit()
    return conn


def add_trip(conn: sqlite3.Connection, trip_id: str, headsign: str,
             direction_id: str, first_dep_s: int, n_stops: int = 5,
             step_s: int = 300) -> None:
    """A trip departing at first_dep_s (seconds since midnight), one stop
    every step_s."""
    conn.execute("INSERT INTO gtfs_trips VALUES (?, '37012', 'wd', ?, ?)",
                 (trip_id, headsign, direction_id))
    for seq in range(1, n_stops + 1):
        t = first_dep_s + (seq - 1) * step_s
        hms = f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"
        conn.execute("INSERT INTO gtfs_stop_times VALUES (?, ?, ?, ?, ?)",
                     (trip_id, hms, hms, f"S{min(seq, 5)}", str(seq)))
    conn.commit()


def obs_row(ts: str, vehicle_id: int, line: str = "37",
            destination: str = "Most SNP", last_stop_order: int | None = None,
            delay: int | None = 0, lat: float = 48.2, lng: float = 17.05) -> dict:
    return {
        "ts": ts, "vehicle_id": vehicle_id, "line": line, "spoj": None,
        "destination": destination, "vehicle_type": "BUS", "is_urban": 1,
        "operator": "DPB", "lat": lat, "lng": lng,
        "last_stop_order": last_stop_order, "is_on_stop": 0,
        "delay_minutes": delay, "license_number": None,
    }
