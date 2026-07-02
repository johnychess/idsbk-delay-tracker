"""Resolve which GTFS service_ids are active on a given date.

Standard GTFS semantics: a service is active when the date falls inside
calendar.txt's [start_date, end_date] with the matching weekday flag set,
then calendar_dates.txt exceptions are applied on top (exception_type 1
adds the service on that date, 2 removes it).

Sanity check from the brief: Mon 2026-06-22 should resolve to
{Prac.dny_0, Prac.dny_11, wv_10}.
"""

from __future__ import annotations

import sqlite3
from datetime import date

WEEKDAY_COLUMNS = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
]


def active_service_ids(conn: sqlite3.Connection, day: date) -> set[str]:
    ymd = day.strftime("%Y%m%d")
    weekday_col = WEEKDAY_COLUMNS[day.weekday()]

    active = {
        row[0]
        for row in conn.execute(
            f"SELECT service_id FROM gtfs_calendar"
            f" WHERE {weekday_col} = '1' AND start_date <= ? AND end_date >= ?",
            (ymd, ymd),
        )
    }
    for service_id, exception_type in conn.execute(
        "SELECT service_id, exception_type FROM gtfs_calendar_dates WHERE date = ?",
        (ymd,),
    ):
        if exception_type == "1":
            active.add(service_id)
        elif exception_type == "2":
            active.discard(service_id)
    return active
