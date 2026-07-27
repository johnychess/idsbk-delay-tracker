"""Trip-splitting, reproduced from the real weekday trace that exposed it.

Line 37, vehicle 803129320, 2026-07-24: one vehicleID held for the whole duty,
stop order cycling and the reported delay jumping ~+13 at every trip boundary
until it reached 920 minutes by evening.
"""

import storage
from analysis import filters, segments
from tests.helpers import obs_row


def test_segments_split_at_order_resets():
    import pandas as pd
    rows = []
    clock, offset = 0, 0
    for _trip in range(3):
        for i, order in enumerate(range(2, 10)):
            hh, mm = divmod(4 * 60 + clock, 60)
            rows.append(obs_row(f"2026-07-24T{hh:02d}:{mm:02d}:00Z", 803129320,
                                last_stop_order=order, delay=offset + i))
            clock += 2
        offset += 13
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["service_date"] = "2026-07-24"

    out = segments.assign_segments(df)
    assert sorted(out["segment"].unique().tolist()) == [0, 1, 2]
    # exactly one trip's worth of observations per segment
    assert out.groupby("segment").size().tolist() == [8, 8, 8]
    # only the first trip's delay level is trustworthy
    assert out[out["absolute_delay_ok"]]["segment"].unique().tolist() == [0]
    assert out["absolute_delay_ok"].sum() == 8

    stats = segments.summarize(out)
    assert stats["runs"] == 1 and stats["trips"] == 3
    assert stats["multi_trip_runs"] == 1
    assert stats["obs_offset_affected"] == 16


def test_single_trip_run_is_fully_usable(tmp_path):
    """The weekend case: one vehicleID = one trip, nothing is discarded."""
    conn = storage.connect(str(tmp_path / "t.sqlite"))
    storage.insert_observations(conn, [
        obs_row(f"2026-07-25T04:{i * 2:02d}:00Z", 555,
                last_stop_order=i + 1, delay=i, lat=48.16 + i * 0.004, lng=17.07)
        for i in range(8)
    ])
    df = filters.clean(filters.load_observations(conn))
    assert (df["segment"] == 0).all()
    assert df["absolute_delay_ok"].all()
    assert df.attrs["filter_report"]["multi_trip_runs"] == 0


def test_offset_trips_survive_the_plausible_cut(tmp_path):
    """The regression: an accumulated offset must not delete the later trips.

    Before splitting, a duty whose delay climbed past MAX_PLAUSIBLE_DELAY_MIN
    lost every observation after that point — which erased weekday line-37
    data from 11:00 onward."""
    conn = storage.connect(str(tmp_path / "t.sqlite"))
    rows, clock, offset = [], 0, 0
    for _trip in range(6):
        for i, order in enumerate(range(2, 10)):
            hh, mm = divmod(4 * 60 + clock, 60)
            rows.append(obs_row(f"2026-07-24T{hh:02d}:{mm:02d}:00Z", 777,
                                last_stop_order=order, delay=offset + i,
                                lat=48.16 + order * 0.004, lng=17.07))
            clock += 2
        offset += 20  # by trip 6 the reported delay is ~100 min
    storage.insert_observations(conn, rows)

    df = filters.clean(filters.load_observations(conn))
    # later trips are kept (they carry usable delay CHANGES) ...
    assert df["segment"].max() == 5
    assert len(df) == 48
    # ... but only the first trip's levels are read as absolute delay
    assert df["absolute_delay_ok"].sum() == 8
