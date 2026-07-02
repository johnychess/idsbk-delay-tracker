"""Matcher tests use 2026-07-01 (a Wednesday); Europe/Bratislava is UTC+2
then, so 06:12 local = 04:12Z."""

from datetime import date

import storage
from match.matcher import match_date, normalize_text
from tests.helpers import add_trip, make_db, obs_row


def test_normalize_text():
    assert normalize_text("Borinka, Staré pece") == "borinka stare pece"
    assert normalize_text("MOST SNP") == "most snp"
    assert normalize_text(None) == ""


def test_match_picks_nearest_trip_and_decodes_poradie(tmp_path):
    conn = make_db(str(tmp_path / "t.sqlite"))
    # two same-direction departures and one opposite-direction decoy
    add_trip(conn, "37012_03_5_18181", "Most SNP", "0", first_dep_s=6 * 3600)
    add_trip(conn, "37012_04_5_18190", "Most SNP", "0", first_dep_s=7 * 3600)
    add_trip(conn, "37012_03_5_19999", "Záhorská Bystrica", "1", first_dep_s=6 * 3600)

    # vehicle at 06:12 local, 2 min late, having passed stop 3 (scheduled
    # 06:10 on the 06:00 trip) -> estimated schedule discrepancy = 0
    storage.insert_observations(conn, [
        obs_row("2026-07-01T04:12:00Z", 111, last_stop_order=3, delay=2),
        obs_row("2026-07-01T04:18:00Z", 111, last_stop_order=4, delay=3),
    ])

    match_date(conn, date(2026, 7, 1), line="37")
    row = conn.execute(
        "SELECT trip_id, poradie, direction_id FROM matched_runs"
        " WHERE service_date='2026-07-01' AND vehicle_id=111").fetchone()
    assert row == ("37012_03_5_18181", "3", "0")


def test_no_match_outside_tolerance(tmp_path):
    conn = make_db(str(tmp_path / "t.sqlite"))
    add_trip(conn, "37012_03_5_18181", "Most SNP", "0", first_dep_s=6 * 3600)

    # observed at 12:00 local — hours away from any scheduled position
    storage.insert_observations(conn, [
        obs_row("2026-07-01T10:00:00Z", 222, last_stop_order=3, delay=1),
    ])
    match_date(conn, date(2026, 7, 1), line="37")
    row = conn.execute(
        "SELECT trip_id FROM matched_runs WHERE vehicle_id=222").fetchone()
    assert row == (None,)


def test_wrong_direction_is_rejected(tmp_path):
    conn = make_db(str(tmp_path / "t.sqlite"))
    # only an opposite-direction trip exists at the right time
    add_trip(conn, "37012_03_5_19999", "Záhorská Bystrica", "1", first_dep_s=6 * 3600)
    storage.insert_observations(conn, [
        obs_row("2026-07-01T04:12:00Z", 333, destination="Most SNP",
                last_stop_order=3, delay=2),
    ])
    match_date(conn, date(2026, 7, 1), line="37")
    row = conn.execute(
        "SELECT trip_id FROM matched_runs WHERE vehicle_id=333").fetchone()
    assert row == (None,)
