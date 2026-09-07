"""Snapshots freeze the report's figures so a later window can be differenced
against them mechanically, and refuse comparisons that would be misleading."""

import json

import pandas as pd
import pytest

import storage
from analysis import route_profile, snapshot
from tests.helpers import make_db, obs_row


def _obs(conn, rows):
    storage.insert_observations(conn, rows)


def test_snapshot_records_the_headline_figures(tmp_path):
    conn = make_db(str(tmp_path / "t.sqlite"))
    _obs(conn, [
        obs_row("2026-07-06T05:00:00Z", 1, last_stop_order=1, delay=0),
        obs_row("2026-07-06T05:04:00Z", 1, last_stop_order=2, delay=2),
        obs_row("2026-07-06T05:08:00Z", 1, last_stop_order=3, delay=4),
    ])
    snap = snapshot.build_snapshot(conn, "37", None, None, label="baseline")

    assert snap["schema_version"] == snapshot.SCHEMA_VERSION
    assert snap["label"] == "baseline"
    assert snap["line"] == "37"
    assert snap["window"]["service_days"] == 1
    assert snap["headline"]["n"] == 3
    assert snap["headline"]["median_delay_min"] == 2.0
    assert snap["punctuality"], "per direction x hour cells must be recorded"
    # everything must survive a JSON round-trip: no numpy types, no NaN
    assert json.loads(json.dumps(snap)) == snap


def test_snapshot_of_an_empty_window_is_still_valid(tmp_path):
    conn = make_db(str(tmp_path / "t.sqlite"))
    snap = snapshot.build_snapshot(conn, "37", None, None)
    assert snap["headline"] == {}
    assert snap["punctuality"] == []
    assert json.loads(json.dumps(snap)) == snap


def test_compare_deltas_baseline_against_current():
    before = {
        "schema_version": snapshot.SCHEMA_VERSION, "line": "37",
        "window": {}, "headline": {"n": 100, "median_delay_min": 2.0,
                                   "pct_on_time": 80.0},
        "punctuality": [{"line": "37", "direction": "Most SNP", "hour": 7,
                         "n": 50, "median": 2.0, "p90": 6.0,
                         "pct_on_time": 70.0}],
        "bottlenecks": [], "inherited": {}, "data_quality": {},
    }
    after = {
        "schema_version": snapshot.SCHEMA_VERSION, "line": "37",
        "window": {}, "headline": {"n": 120, "median_delay_min": 5.0,
                                   "pct_on_time": 61.0},
        "punctuality": [{"line": "37", "direction": "Most SNP", "hour": 7,
                         "n": 60, "median": 5.0, "p90": 12.0,
                         "pct_on_time": 52.0}],
        "bottlenecks": [], "inherited": {}, "data_quality": {},
    }
    diffs = snapshot.compare(before, after)

    head = diffs["headline"].set_index("metric")
    assert head.loc["median_delay_min", "delta"] == 3.0
    assert head.loc["pct_on_time", "delta"] == -19.0

    cell = diffs["punctuality"].iloc[0]
    assert cell["median_delta"] == 3.0
    assert cell["pct_on_time_delta"] == -18.0

    text = snapshot.compare_markdown(before, after)
    assert "Most SNP" in text and "Headline" in text


def test_compare_keeps_cells_present_on_only_one_side():
    """A direction x hour that exists in one window and not the other is a
    finding (service added or withdrawn), not a row to drop silently."""
    base = {"schema_version": snapshot.SCHEMA_VERSION, "line": "37",
            "window": {}, "headline": {}, "inherited": {}, "data_quality": {},
            "bottlenecks": []}
    before = dict(base, punctuality=[
        {"line": "37", "direction": "Most SNP", "hour": 4, "n": 10,
         "median": 0.0, "p90": 1.0, "pct_on_time": 100.0}])
    after = dict(base, punctuality=[
        {"line": "37", "direction": "Most SNP", "hour": 23, "n": 8,
         "median": 1.0, "p90": 3.0, "pct_on_time": 90.0}])

    hours = set(snapshot.compare(before, after)["punctuality"]["hour"])
    assert hours == {4, 23}


def test_compare_refuses_mismatched_schema_or_line():
    a = {"schema_version": 1, "line": "37"}
    with pytest.raises(ValueError, match="schema mismatch"):
        snapshot.compare(a, {"schema_version": 2, "line": "37"})
    with pytest.raises(ValueError, match="different lines"):
        snapshot.compare(a, {"schema_version": 1, "line": "3"})


def test_save_and_load_round_trip(tmp_path):
    path = str(tmp_path / "nested" / "snap.json")
    snap = {"schema_version": snapshot.SCHEMA_VERSION, "line": "37",
            "label": "školské prázdniny"}
    snapshot.save(snap, path)
    assert snapshot.load(path) == snap  # incl. non-ASCII label


def test_report_writes_a_snapshot_beside_the_markdown(tmp_path):
    conn = make_db(str(tmp_path / "t.sqlite"))
    _obs(conn, [
        obs_row("2026-07-06T05:00:00Z", 1, last_stop_order=1, delay=0),
        obs_row("2026-07-06T05:04:00Z", 1, last_stop_order=2, delay=1),
    ])
    conn.close()

    from analysis import report
    out = str(tmp_path / "out")
    report.build_report(str(tmp_path / "t.sqlite"), "37", None, None, out,
                        label="holiday")
    snap = snapshot.load(str(tmp_path / "out" / "snapshot.json"))
    assert snap["label"] == "holiday"
    assert snap["headline"]["n"] == 2


# --- bottleneck distance normalisation -------------------------------------

def _traversal(from_order, to_order, increment, lat_a, lng_a, lat_b, lng_b):
    from analysis.filters import haversine_m
    return {
        "line": "37", "direction": "Most SNP",
        "from_order": from_order, "to_order": to_order,
        "span": to_order - from_order, "delay_increment": increment,
        "mid_lat": (lat_a + lat_b) / 2, "mid_lng": (lng_a + lng_b) / 2,
        "distance_km": haversine_m(lat_a, lng_a, lat_b, lng_b) / 1000.0,
        "hour": 7, "service_date": "2026-07-06", "vehicle_id": 1,
    }


def test_per_km_separates_a_slow_segment_from_a_long_one():
    """The open question on the July report: is a 3.3 min/stop segment slow,
    or are its two stops simply far apart? Two segments lose the same 3 min
    per stop; one covers 300 m, the other 3 km. Per-stop cannot tell them
    apart, per-km must."""
    short_slow = [_traversal(11, 12, 3.0, 48.180, 17.050, 48.1827, 17.050)
                  for _ in range(12)]
    long_normal = [_traversal(1, 2, 3.0, 48.140, 17.090, 48.167, 17.090)
                   for _ in range(12)]
    table = route_profile.bottleneck_table(
        pd.DataFrame(short_slow + long_normal))

    by_seg = table.set_index("from_order")
    # identical on the per-stop ranking the report leads with
    assert by_seg.loc[11, "mean_increment"] == by_seg.loc[1, "mean_increment"]
    # but the stop spacing is an order of magnitude apart
    assert by_seg.loc[11, "km_per_stop"] < 0.5
    assert by_seg.loc[1, "km_per_stop"] > 2.5
    # ... so per-km ranks the genuinely slow one far above the merely long one
    assert by_seg.loc[11, "increment_per_km"] > 5 * by_seg.loc[1, "increment_per_km"]
    assert route_profile.by_distance(table).iloc[0]["from_order"] == 11


def test_gps_jitter_is_excluded_from_the_per_km_statistic():
    """Two fixes metres apart would divide a real delay by noise. Such rows
    still count towards min/stop; they must not reach min/km."""
    jitter = [_traversal(5, 6, 2.0, 48.2000, 17.0500, 48.20005, 17.05001)
              for _ in range(12)]
    table = route_profile.bottleneck_table(pd.DataFrame(jitter))
    row = table.iloc[0]
    assert row["n"] == 12                 # counted per stop
    assert row["n_km"] == 0               # excluded from per km
    assert pd.isna(row["increment_per_km"])
    assert route_profile.by_distance(table).empty


def test_increments_carry_distance_and_survive_missing_column():
    """by_distance must degrade quietly on a table built before distances
    existed rather than raising."""
    legacy = pd.DataFrame([{"line": "37", "direction": "d", "from_order": 1,
                            "to_order": 2, "n": 20, "mean_increment": 1.0}])
    assert route_profile.by_distance(legacy).empty
