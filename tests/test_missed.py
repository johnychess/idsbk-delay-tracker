"""Missed-departure verdicts, 2026-07-01 (Wednesday, UTC+2).

Four scheduled departures:
  T1 06:00 — matched to a run            -> served
  T2 08:00 — unmatched, but a same-line vehicle left the origin in-window
                                          -> served_probably
  T3 10:00 — nothing observed, sweeps prove full coverage -> missed
  T4 12:00 — nothing observed, collector was down          -> unknown
"""

from datetime import date, datetime, timedelta, timezone

import storage
from analysis import missed
from tests.helpers import add_trip, make_db, obs_row


def _add_sweeps(conn, start_utc: datetime, end_utc: datetime):
    t = start_utc
    while t <= end_utc:
        storage.record_sweep(conn, t.strftime("%Y-%m-%dT%H:%M:%SZ"), 16, 0, 300, 20.0)
        t += timedelta(seconds=120)


def test_verdicts(tmp_path):
    conn = make_db(str(tmp_path / "t.sqlite"))
    add_trip(conn, "37012_01_5_1", "Most SNP", "0", first_dep_s=6 * 3600)
    add_trip(conn, "37012_02_5_2", "Most SNP", "0", first_dep_s=8 * 3600)
    add_trip(conn, "37012_03_5_3", "Most SNP", "0", first_dep_s=10 * 3600)
    add_trip(conn, "37012_04_5_4", "Most SNP", "0", first_dep_s=12 * 3600)

    conn.execute(
        "INSERT INTO matched_runs (service_date, vehicle_id, line, trip_id,"
        " matched_at) VALUES ('2026-07-01', 1, '37', '37012_01_5_1', 'x')")

    # T2: an origin observation inside [07:55, 08:25] local = [05:55, 06:25]Z
    storage.insert_observations(
        conn, [obs_row("2026-07-01T06:05:00Z", 2, last_stop_order=1, delay=1)])

    # T3's window [09:55, 10:25] local = [07:55, 08:25]Z fully swept
    _add_sweeps(conn,
                datetime(2026, 7, 1, 7, 54, tzinfo=timezone.utc),
                datetime(2026, 7, 1, 8, 26, tzinfo=timezone.utc))
    # T4: no sweeps at all around 12:00 local

    verdicts = missed.missed_departures(conn, date(2026, 7, 1), "37")
    by_trip = dict(zip(verdicts["trip_id"], verdicts["verdict"]))
    assert by_trip == {
        "37012_01_5_1": "served",
        "37012_02_5_2": "served_probably",
        "37012_03_5_3": "missed",
        "37012_04_5_4": "unknown",
    }

    summary = missed.summarize(verdicts)
    assert summary["scheduled_total"] == 4
    assert summary["missed"] == 1
