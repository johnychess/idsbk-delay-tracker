import storage
from analysis import filters
from tests.helpers import obs_row


def test_clean_drops_implausible_and_stale(tmp_path):
    conn = storage.connect(str(tmp_path / "t.sqlite"))
    rows = []
    # a normal moving run: advances stops, plausible delay
    for i in range(6):
        rows.append(obs_row(f"2026-07-01T04:{10 + i * 2:02d}:00Z", 1,
                            last_stop_order=i + 1, delay=4,
                            lat=48.20 + i * 0.005, lng=17.05))
    # an implausible stale-trip delay (parked tram syndrome, 500+ min)
    rows.append(obs_row("2026-07-01T04:10:00Z", 2, line="1",
                        last_stop_order=9, delay=512))
    # a parked vehicle: 6 obs over 40 min, same spot, order never advances
    for i in range(6):
        rows.append(obs_row(f"2026-07-01T05:{i * 8:02d}:00Z", 3,
                            last_stop_order=7, delay=30,
                            lat=48.21000, lng=17.06000))
    storage.insert_observations(conn, rows)

    df = filters.load_observations(conn)
    assert len(df) == 13
    cleaned = filters.clean(df)

    assert set(cleaned["vehicle_id"].unique()) == {1}
    report = cleaned.attrs["filter_report"]
    assert report["dropped_implausible_delay"] == 1
    assert report["dropped_stale_parked"] == 6
    assert report["kept"] == 6


def test_dead_trip_detection_catches_drifting_parked_vehicle(tmp_path):
    """A finished vehicle keeps its trip: delay grows ~1 min per minute of
    wall clock. It drifts >150m so the displacement test misses it — this is
    the 60-90 min band that was leaking into the reports."""
    conn = storage.connect(str(tmp_path / "t.sqlite"))
    rows = []
    # dead trip: 10 obs over 45 min, delay 40->85, wandering ~600m
    for i in range(10):
        rows.append(obs_row(f"2026-07-01T09:{i * 5:02d}:00Z", 7,
                            last_stop_order=12, delay=40 + i * 5,
                            lat=48.2000 + i * 0.0006, lng=17.05))
    # genuinely late but recovering run: delay does NOT track the clock
    for i in range(10):
        rows.append(obs_row(f"2026-07-01T09:{i * 5:02d}:00Z", 8,
                            last_stop_order=i + 1, delay=25 - i,
                            lat=48.16 + i * 0.004, lng=17.07))
    storage.insert_observations(conn, rows)

    df = filters.load_observations(conn)
    dead = filters.flag_dead_trips(df)
    assert set(df[dead]["vehicle_id"].unique()) == {7}

    cleaned = filters.clean(df)
    assert set(cleaned["vehicle_id"].unique()) == {8}
    assert cleaned.attrs["filter_report"]["dropped_dead_trip"] == 10


def test_load_observations_derives_local_fields(tmp_path):
    conn = storage.connect(str(tmp_path / "t.sqlite"))
    storage.insert_observations(
        conn, [obs_row("2026-07-01T04:12:00Z", 1, last_stop_order=1)])
    df = filters.load_observations(conn)
    assert df.iloc[0]["service_date"] == "2026-07-01"
    assert df.iloc[0]["hour"] == 6      # UTC+2 in July
    assert df.iloc[0]["weekday"] == 2   # Wednesday
