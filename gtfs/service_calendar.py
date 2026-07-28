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
from datetime import date, timedelta

WEEKDAY_COLUMNS = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
]


def resolve_service_date(conn: sqlite3.Connection, day: date,
                         feed_id: str) -> date:
    """The date to look service patterns up under, for a feed being used as a
    stand-in.

    A borrowed feed's calendar rows are bounded by its own validity window, so
    asking it about a date outside that window yields no services at all —
    selecting the feed is not enough. Map the date onto the NEAREST date the
    feed does cover that falls on the same weekday (3 Jul Friday -> 24 Jul
    Friday). That preserves the weekday service pattern and cannot mix two
    seasons together, which a plain weekday-flag lookup would risk when a feed
    carries several date-bounded service sets.

    Returns `day` unchanged when the feed already covers it.

    Caveat: public-holiday exceptions belong to actual dates, so a borrowed
    date inherits the surrogate's holiday status, not its own."""
    row = conn.execute(
        "SELECT start_date, end_date FROM gtfs_feeds WHERE feed_id = ?",
        (feed_id,),
    ).fetchone()
    if not row or not row[0] or not row[1]:
        return day
    try:
        start = date(int(row[0][:4]), int(row[0][4:6]), int(row[0][6:8]))
        end = date(int(row[1][:4]), int(row[1][4:6]), int(row[1][6:8]))
    except (ValueError, TypeError):
        return day
    if start <= day <= end:
        return day
    if day < start:
        # first covered date on the same weekday
        return start + timedelta(days=(day.weekday() - start.weekday()) % 7)
    # last covered date on the same weekday
    return end - timedelta(days=(end.weekday() - day.weekday()) % 7)


def active_service_ids(conn: sqlite3.Connection, day: date,
                       feed_id: str | None = None) -> set[str]:
    """Services running on `day`. Scoped to one archived feed when feed_id is
    given — required once feeds accumulate, since each feed only declares
    service for its own validity window."""
    ymd = day.strftime("%Y%m%d")
    weekday_col = WEEKDAY_COLUMNS[day.weekday()]
    feed_clause = " AND feed_id = ?" if feed_id else ""
    feed_args = (feed_id,) if feed_id else ()

    active = {
        row[0]
        for row in conn.execute(
            f"SELECT service_id FROM gtfs_calendar"
            f" WHERE {weekday_col} = '1' AND start_date <= ? AND end_date >= ?"
            f"{feed_clause}",
            (ymd, ymd, *feed_args),
        )
    }
    for service_id, exception_type in conn.execute(
        f"SELECT service_id, exception_type FROM gtfs_calendar_dates"
        f" WHERE date = ?{feed_clause}",
        (ymd, *feed_args),
    ):
        if exception_type == "1":
            active.add(service_id)
        elif exception_type == "2":
            active.discard(service_id)
    return active
