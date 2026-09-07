"""The collector's nightly matching pass.

Matching used to be a manual step, and matched_runs fell 40 days behind the
observations without anything failing — the analyses that need it degrade
quietly. These tests pin the schedule rules that keep it current."""

from datetime import date, datetime, timedelta

import pytest

import config
import storage
from collector import main as collector_main
from tests.helpers import add_trip, make_db, obs_row


@pytest.fixture
def db(tmp_path):
    conn = make_db(str(tmp_path / "t.sqlite"))
    add_trip(conn, "37012_03_5_18181", "Most SNP", "0", first_dep_s=6 * 3600)
    return conn


def _at(monkeypatch, when: datetime):
    monkeypatch.setattr(collector_main, "_now_local",
                        lambda: when.replace(tzinfo=config.LOCAL_TZ))


def _observe(conn, day: date, vehicle_id: int = 111):
    storage.insert_observations(conn, [
        obs_row(f"{day.isoformat()}T04:12:00Z", vehicle_id,
                last_stop_order=3, delay=2),
        obs_row(f"{day.isoformat()}T04:18:00Z", vehicle_id,
                last_stop_order=4, delay=3),
    ])


def _matched_dates(conn) -> set[str]:
    return {row[0] for row in conn.execute(
        "SELECT DISTINCT service_date FROM matched_runs")}


def test_pass_matches_yesterday_but_never_today(db, monkeypatch):
    """A day still in progress would be matched against a partial set of runs,
    so the lookback starts at yesterday."""
    today = date(2026, 9, 8)
    _observe(db, today, vehicle_id=1)
    _observe(db, today - timedelta(days=1), vehicle_id=2)

    _at(monkeypatch, datetime(2026, 9, 8, 3, 0))
    collector_main._maybe_match_recent(db)

    matched = _matched_dates(db)
    assert (today - timedelta(days=1)).isoformat() in matched
    assert today.isoformat() not in matched


def test_pass_does_not_run_before_its_hour(db, monkeypatch):
    _observe(db, date(2026, 9, 7))
    _at(monkeypatch, datetime(2026, 9, 8, 2, 59))
    collector_main._maybe_match_recent(db)
    assert _matched_dates(db) == set()


def test_pass_runs_once_per_day(db, monkeypatch):
    """The marker must stop a 120-second loop re-matching all night."""
    _observe(db, date(2026, 9, 7))
    _at(monkeypatch, datetime(2026, 9, 8, 3, 0))
    collector_main._maybe_match_recent(db)
    assert storage.get_meta(db, "match_checked_2026-09-08") == "1"

    calls = []
    monkeypatch.setattr(collector_main.matcher, "match_date",
                        lambda *a, **k: calls.append(a) or 0)
    _at(monkeypatch, datetime(2026, 9, 8, 3, 2))
    collector_main._maybe_match_recent(db)
    assert calls == []

    # ... and the next night is a fresh marker, so it runs again
    _at(monkeypatch, datetime(2026, 9, 9, 3, 0))
    collector_main._maybe_match_recent(db)
    assert calls, "a new day must trigger a new pass"


def test_lookback_recovers_days_missed_during_downtime(db, monkeypatch):
    """Re-matching is an idempotent upsert, so the lookback is what picks up a
    day whose feed landed late or that was missed while the collector was
    down."""
    for delta in (1, 2, 3):
        _observe(db, date(2026, 9, 8) - timedelta(days=delta),
                 vehicle_id=100 + delta)

    monkeypatch.setattr(config, "MATCH_LOOKBACK_DAYS", 3)
    _at(monkeypatch, datetime(2026, 9, 8, 3, 0))
    collector_main._maybe_match_recent(db)

    assert _matched_dates(db) == {"2026-09-05", "2026-09-06", "2026-09-07"}


def test_rerunning_a_matched_day_does_not_duplicate_rows(db, monkeypatch):
    _observe(db, date(2026, 9, 7))
    _at(monkeypatch, datetime(2026, 9, 8, 3, 0))
    collector_main._maybe_match_recent(db)
    before = db.execute("SELECT COUNT(*) FROM matched_runs").fetchone()[0]

    storage.set_meta(db, "match_checked_2026-09-08", "")
    collector_main._maybe_match_recent(db)
    assert db.execute("SELECT COUNT(*) FROM matched_runs").fetchone()[0] == before


def test_a_failing_day_does_not_abort_the_rest_or_the_collector(db, monkeypatch):
    """One bad day must not cost the others, nor take the loop down."""
    for delta in (1, 2):
        _observe(db, date(2026, 9, 8) - timedelta(days=delta),
                 vehicle_id=200 + delta)

    real = collector_main.matcher.match_date
    seen = []

    def flaky(conn, day, line=None):
        seen.append(day)
        if day == date(2026, 9, 7):
            raise RuntimeError("schedule lookup exploded")
        return real(conn, day, line=line)

    monkeypatch.setattr(config, "MATCH_LOOKBACK_DAYS", 2)
    monkeypatch.setattr(collector_main.matcher, "match_date", flaky)
    _at(monkeypatch, datetime(2026, 9, 8, 3, 0))
    collector_main._maybe_match_recent(db)  # must not raise

    assert seen == [date(2026, 9, 7), date(2026, 9, 6)]
    assert _matched_dates(db) == {"2026-09-06"}
    # the pass still completes, so it is not retried in a hot loop tonight
    assert storage.get_meta(db, "match_checked_2026-09-08") == "1"


def test_disabling_the_pass_is_honoured(db, monkeypatch):
    _observe(db, date(2026, 9, 7))
    monkeypatch.setattr(config, "MATCH_ENABLED", False)
    _at(monkeypatch, datetime(2026, 9, 8, 3, 0))
    collector_main._maybe_match_recent(db)
    assert _matched_dates(db) == set()


def test_every_configured_line_is_matched(db, monkeypatch):
    _observe(db, date(2026, 9, 7))
    calls = []
    monkeypatch.setattr(collector_main.matcher, "match_date",
                        lambda conn, day, line=None: calls.append(line) or 0)
    monkeypatch.setattr(config, "MATCH_LINES", ["37", "3"])
    monkeypatch.setattr(config, "MATCH_LOOKBACK_DAYS", 1)
    _at(monkeypatch, datetime(2026, 9, 8, 3, 0))
    collector_main._maybe_match_recent(db)
    assert calls == ["37", "3"]


def test_empty_line_list_means_every_line(db, monkeypatch):
    _observe(db, date(2026, 9, 7))
    calls = []
    monkeypatch.setattr(collector_main.matcher, "match_date",
                        lambda conn, day, line=None: calls.append(line) or 0)
    monkeypatch.setattr(config, "MATCH_LINES", [])
    monkeypatch.setattr(config, "MATCH_LOOKBACK_DAYS", 1)
    _at(monkeypatch, datetime(2026, 9, 8, 3, 0))
    collector_main._maybe_match_recent(db)
    assert calls == [None]
