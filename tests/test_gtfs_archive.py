"""GTFS feed archiving: feeds accumulate instead of overwriting, each date
resolves to the feed that was valid then, and a date with no feed never
destroys existing matches."""

from datetime import date

import storage
from gtfs.service_calendar import active_service_ids, resolve_service_date
from match.matcher import match_date
from tests.helpers import add_trip, make_db, obs_row


def _register(conn, feed_id, start, end, downloaded):
    storage.register_gtfs_feed(conn, feed_id, downloaded, start, end, "u", 1)


def test_feed_for_date_picks_the_feed_valid_then(tmp_path):
    conn = storage.connect(str(tmp_path / "t.sqlite"))
    _register(conn, "spring", "20260101", "20260723", "2026-01-01T00:00:00+01:00")
    _register(conn, "summer", "20260724", "20261231", "2026-07-24T00:00:00+02:00")

    # the exact failure that orphaned July 3-23: an old date must resolve to
    # the old feed, not to whatever was downloaded most recently
    assert storage.feed_for_date(conn, date(2026, 7, 15)) == ("spring", True)
    assert storage.feed_for_date(conn, date(2026, 7, 25)) == ("summer", True)
    assert storage.feed_for_date(conn, date(2026, 7, 23)) == ("spring", True)
    assert storage.feed_for_date(conn, date(2026, 7, 24)) == ("summer", True)
    # nothing covers 2025 -> unknown, not "no service"
    assert storage.feed_for_date(conn, date(2025, 5, 1)) == (None, False)


def test_overlapping_feeds_prefer_the_later_revision(tmp_path):
    conn = storage.connect(str(tmp_path / "t.sqlite"))
    _register(conn, "old", "20260101", "20261231", "2026-01-01T00:00:00+01:00")
    _register(conn, "revised", "20260601", "20261231", "2026-06-01T00:00:00+02:00")
    assert storage.feed_for_date(conn, date(2026, 7, 1)) == ("revised", True)
    assert storage.feed_for_date(conn, date(2026, 3, 1)) == ("old", True)


def test_nearest_feed_fallback_is_flagged_and_bounded(tmp_path):
    """Dates whose feed was overwritten before archiving existed can borrow a
    neighbouring feed — but only a nearby one, and never silently."""
    conn = storage.connect(str(tmp_path / "t.sqlite"))
    _register(conn, "summer", "20260724", "20261231", "2026-07-24T00:00:00+02:00")

    # 2026-07-15 is 9 days before the feed starts: same holiday season, usable
    assert storage.feed_for_date(conn, date(2026, 7, 15)) == (None, False)
    assert storage.feed_for_date(
        conn, date(2026, 7, 15), allow_nearest=True) == ("summer", False)

    # a date months away is a different timetable — refuse it
    assert storage.feed_for_date(
        conn, date(2026, 1, 5), allow_nearest=True, max_gap_days=45) == (None, False)
    # ... and the bound is applied to the near case too
    assert storage.feed_for_date(
        conn, date(2026, 7, 15), allow_nearest=True, max_gap_days=45) == ("summer", False)


def test_match_date_without_feed_does_not_destroy_existing_matches(tmp_path):
    """The July-24 regression: re-matching an uncovered date used to overwrite
    good trip_ids with NULL. It must now skip the date untouched."""
    conn = make_db(str(tmp_path / "t.sqlite"))
    add_trip(conn, "37012_03_5_18181", "Most SNP", "0", first_dep_s=6 * 3600)
    storage.insert_observations(conn, [
        obs_row("2026-07-01T04:12:00Z", 111, last_stop_order=3, delay=2),
        obs_row("2026-07-01T04:18:00Z", 111, last_stop_order=4, delay=3),
    ])
    assert match_date(conn, date(2026, 7, 1), line="37") == 1  # one run, two obs
    before = conn.execute(
        "SELECT trip_id FROM matched_runs WHERE vehicle_id=111").fetchone()
    assert before[0] == "37012_03_5_18181"

    # Move every feed far away, so not even the nearest-feed fallback applies.
    conn.execute("UPDATE gtfs_feeds SET start_date='20270101', end_date='20271231'")
    conn.commit()

    assert match_date(conn, date(2026, 7, 1), line="37") == 0  # skipped
    after = conn.execute(
        "SELECT trip_id FROM matched_runs WHERE vehicle_id=111").fetchone()
    assert after[0] == "37012_03_5_18181"  # preserved, not NULLed


def test_resolve_service_date_maps_to_nearest_same_weekday():
    """A stand-in feed's calendar is bounded by its own validity window, so a
    borrowed date must be looked up under a date the feed actually covers —
    on the same weekday, or the service pattern would be wrong."""
    conn = storage.connect(":memory:")
    _register(conn, "summer", "20260724", "20261231", "2026-07-24T00:00:00+02:00")

    # 2026-07-03 is a Friday; the first Friday the feed covers is 2026-07-24
    assert resolve_service_date(conn, date(2026, 7, 3), "summer") == date(2026, 7, 24)
    # 2026-07-04 Saturday -> 2026-07-25 Saturday
    assert resolve_service_date(conn, date(2026, 7, 4), "summer") == date(2026, 7, 25)
    # a covered date is returned untouched
    assert resolve_service_date(conn, date(2026, 8, 3), "summer") == date(2026, 8, 3)
    # past the end: map back to the last covered date of that weekday
    after = resolve_service_date(conn, date(2027, 3, 10), "summer")
    assert after <= date(2026, 12, 31)
    assert after.weekday() == date(2027, 3, 10).weekday()


def test_nearby_feed_recovers_the_date_and_marks_it_approximate(tmp_path):
    """Dates orphaned by the pre-archiving overwrite (July 3-23) are matched
    against a neighbouring feed rather than lost — flagged, never silent.

    The calendar is bounded to the feed's own window here, reproducing the real
    condition: selecting the feed alone yields zero services and zero matches."""
    conn = make_db(str(tmp_path / "t.sqlite"))
    add_trip(conn, "37012_03_5_18181", "Most SNP", "0", first_dep_s=6 * 3600)
    storage.insert_observations(conn, [
        # 2026-07-03 is a Friday, mapped onto Friday 2026-07-24
        obs_row("2026-07-03T04:12:00Z", 111, last_stop_order=3, delay=2),
        obs_row("2026-07-03T04:18:00Z", 111, last_stop_order=4, delay=3),
    ])
    conn.execute("UPDATE gtfs_feeds SET start_date='20260724', end_date='20261231'")
    conn.execute("UPDATE gtfs_calendar SET start_date='20260724', end_date='20261231'")
    conn.commit()

    # sanity: the borrowed date has no services under its own calendar window
    assert active_service_ids(conn, date(2026, 7, 3), feed_id="testfeed") == set()

    assert match_date(conn, date(2026, 7, 3), line="37") == 1
    trip_id, feed_exact = conn.execute(
        "SELECT trip_id, feed_exact FROM matched_runs WHERE vehicle_id=111"
    ).fetchone()
    assert trip_id == "37012_03_5_18181"   # recovered
    assert feed_exact == 0                 # and marked approximate


def test_two_feeds_coexist_without_clobbering(tmp_path):
    """Rows from an older feed survive loading a newer one."""
    conn = make_db(str(tmp_path / "t.sqlite"))
    add_trip(conn, "37012_03_5_111", "Most SNP", "0", first_dep_s=6 * 3600)
    conn.execute("INSERT INTO gtfs_trips VALUES"
                 " ('newfeed_trip', '37012', 'wd', 'Most SNP', '0', 'newfeed')")
    conn.commit()
    per_feed = dict(conn.execute(
        "SELECT feed_id, COUNT(*) FROM gtfs_trips GROUP BY feed_id"))
    assert per_feed["testfeed"] == 1
    assert per_feed["newfeed"] == 1
