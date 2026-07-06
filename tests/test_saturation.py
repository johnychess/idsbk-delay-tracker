import json

import storage
from collector.sweep import VEHICLE_CAP, summarize_points


def test_summarize_points():
    # max, and count of points at the 100-vehicle cap
    assert summarize_points([10, 100, 100, None, 50]) == (100, 2)
    assert summarize_points([None, None]) == (0, 0)
    assert summarize_points([99, 100]) == (100, 1)
    assert summarize_points([]) == (0, 0)
    assert VEHICLE_CAP == 100


def test_record_sweep_stores_saturation(tmp_path):
    conn = storage.connect(str(tmp_path / "t.sqlite"))
    storage.record_sweep(
        conn, "2026-07-06T08:00:00Z", 25, 0, 303, 22.5,
        max_point_count=100, points_at_cap=4, point_counts=json.dumps([100, 80, 100]),
    )
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
