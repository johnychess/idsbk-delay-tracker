from datetime import date

import storage
from gtfs.loader import gtfs_time_to_seconds
from gtfs.poradie import normalize_poradie, poradie_from_trip_id
from gtfs.service_calendar import active_service_ids


def test_poradie_from_trip_id():
    assert poradie_from_trip_id("37012_03_5_18181") == "03"
    assert poradie_from_trip_id("37012_51_1_99") == "51"
    assert poradie_from_trip_id("weird") is None
    assert poradie_from_trip_id("a__b") is None


def test_normalize_poradie():
    assert normalize_poradie("03") == "3"
    assert normalize_poradie("3") == "3"
    assert normalize_poradie("2a") == "2a"
    assert normalize_poradie("02B") == "2b"
    assert normalize_poradie("0") == "0"
    assert normalize_poradie(None) is None


def test_gtfs_time_to_seconds():
    assert gtfs_time_to_seconds("06:30:00") == 6 * 3600 + 30 * 60
    assert gtfs_time_to_seconds("26:15:00") == 26 * 3600 + 15 * 60  # after midnight
    assert gtfs_time_to_seconds("") is None
    assert gtfs_time_to_seconds(None) is None


def test_active_service_ids(tmp_path):
    conn = storage.connect(str(tmp_path / "t.sqlite"))
    conn.execute(
        "CREATE TABLE gtfs_calendar (service_id TEXT, monday TEXT, tuesday TEXT,"
        " wednesday TEXT, thursday TEXT, friday TEXT, saturday TEXT, sunday TEXT,"
        " start_date TEXT, end_date TEXT)")
    conn.execute(
        "CREATE TABLE gtfs_calendar_dates (service_id TEXT, date TEXT, exception_type TEXT)")
    conn.execute("INSERT INTO gtfs_calendar VALUES"
                 " ('workdays','1','1','1','1','1','0','0','20260601','20260831')")
    conn.execute("INSERT INTO gtfs_calendar VALUES"
                 " ('weekend','0','0','0','0','0','1','1','20260601','20260831')")
    conn.execute("INSERT INTO gtfs_calendar VALUES"
                 " ('expired','1','1','1','1','1','1','1','20250101','20251231')")
    # a holiday: workdays removed, weekend added, on a Monday
    conn.execute("INSERT INTO gtfs_calendar_dates VALUES ('workdays','20260622','2')")
    conn.execute("INSERT INTO gtfs_calendar_dates VALUES ('weekend','20260622','1')")

    assert active_service_ids(conn, date(2026, 6, 23)) == {"workdays"}  # Tuesday
    assert active_service_ids(conn, date(2026, 6, 27)) == {"weekend"}   # Saturday
    assert active_service_ids(conn, date(2026, 6, 22)) == {"weekend"}   # exception day
