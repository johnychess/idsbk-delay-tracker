"""GTFS feed archiving: feeds accumulate instead of overwriting, each date
resolves to the feed that was valid then, and a date with no feed never
destroys existing matches."""

from datetime import date

import storage
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
    assert storage.feed_for_date(conn, date(2026, 7, 15)) == "spring"
    assert storage.feed_for_date(conn, date(2026, 7, 25)) == "summer"
    assert storage.feed_for_date(conn, date(2026, 7, 23)) == "spring"
    assert storage.feed_for_date(conn, date(2026, 7, 24)) == "summer"
    # nothing covers 2025 -> unknown, not "no service"
    assert storage.feed_for_date(conn, date(2025, 5, 1)) is None


def test_overlapping_feeds_prefer_the_later_revision(tmp_path):
    conn = storage.connect(str(tmp_path / "t.sqlite"))
    _register(conn, "old", "20260101", "20261231", "2026-01-01T00:00:00+01:00")
    _register(conn, "revised", "20260601", "20261231", "2026-06-01T00:00:00+02:00")
    assert storage.feed_for_date(conn, date(2026, 7, 1)) == "revised"
    assert storage.feed_for_date(conn, date(2026, 3, 1)) == "old"


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

    # drop the feed's coverage of that date, as a GTFS refresh used to
    conn.execute("UPDATE gtfs_feeds SET start_date='20260724'")
    conn.commit()

    assert match_date(conn, date(2026, 7, 1), line="37") == 0  # skipped
    after = conn.execute(
        "SELECT trip_id FROM matched_runs WHERE vehicle_id=111").fetchone()
    assert after[0] == "37012_03_5_18181"  # preserved, not NULLed


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
