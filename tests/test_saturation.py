import json

import storage
from collector.sweep import VEHICLE_CAP, summarize_points


def test_summarize_points_counts_and_cap():
    s = summarize_points([10, 100, 100, None, 50])
    assert s["max_point_count"] == 100
    assert s["points_at_cap"] == 2
    assert summarize_points([None, None])["max_point_count"] == 0
    assert summarize_points([])["points_at_cap"] == 0
    assert VEHICLE_CAP == 100


def test_hitting_the_cap_is_not_by_itself_coverage_loss():
    """The old metric's flaw: with a few hundred vehicles in a compact city
    every point returns its full 100 at any hour, so `points_at_cap` tracked
    fleet size, not lost data. A capped point that still sees past the midpoint
    to its neighbour has lost nothing."""
    nn = [4.0, 4.0]                 # neighbours 4 km apart -> need 2 km reach
    s = summarize_points([100, 100], reaches=[5.0, 6.0], nn_km=nn)
    assert s["points_at_cap"] == 2          # both capped ...
    assert s["points_undercovering"] == 0   # ... but neither lost anything
    assert s["min_reach_margin_km"] == 3.0


def test_undercovering_is_flagged_when_a_capped_point_cannot_reach_its_cell():
    nn = [4.0, 4.0]                 # need 2 km reach
    s = summarize_points([100, 100], reaches=[1.2, 5.0], nn_km=nn)
    assert s["points_undercovering"] == 1   # the 1.2 km one leaves a gap
    assert s["min_reach_margin_km"] == -0.8


def test_short_reach_without_the_cap_is_fine():
    """A point that returned fewer than 100 saw everything near it; a small
    reach just means the area is empty, not that data was truncated."""
    s = summarize_points([12], reaches=[0.5], nn_km=[4.0])
    assert s["points_undercovering"] == 0


def test_record_sweep_stores_saturation(tmp_path):
    conn = storage.connect(str(tmp_path / "t.sqlite"))
    storage.record_sweep(
        conn, "2026-07-06T08:00:00Z", 25, 0, 303, 22.5,
        max_point_count=100, points_at_cap=4, points_undercovering=1,
        min_reach_margin_km=-0.4, point_counts=json.dumps([100, 80, 100]),
        point_reach_km=json.dumps([1.2, 5.0, 4.4]),
    )
    assert conn.execute(
        "SELECT points_undercovering, min_reach_margin_km FROM sweeps"
    ).fetchone() == (1, -0.4)
    row = conn.execute(
        "SELECT max_point_count, points_at_cap, point_counts FROM sweeps"
    ).fetchone()
    assert row[0] == 100
    assert row[1] == 4
    assert json.loads(row[2]) == [100, 80, 100]


def test_migration_adds_columns_to_old_db(tmp_path):
    # Simulate a pre-saturation DB: sweeps without the new columns.
    db = str(tmp_path / "old.sqlite")
    import sqlite3
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE sweeps (ts TEXT, points_queried INTEGER,"
                " points_failed INTEGER, vehicles_seen INTEGER, duration_s REAL)")
    raw.execute("INSERT INTO sweeps VALUES ('2026-07-01T00:00:00Z', 16, 0, 85, 19.0)")
    raw.commit()
    raw.close()

    # Opening via storage.connect must migrate it and preserve the old row.
    conn = storage.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sweeps)")}
    assert {"max_point_count", "points_at_cap", "point_counts"} <= cols
    assert conn.execute("SELECT vehicles_seen FROM sweeps").fetchone()[0] == 85
    # New writes work against the migrated table.
    storage.record_sweep(conn, "2026-07-06T08:00:00Z", 25, 0, 303, 22.5,
                         max_point_count=100, points_at_cap=4, point_counts="[100]")
    assert conn.execute("SELECT COUNT(*) FROM sweeps").fetchone()[0] == 2
