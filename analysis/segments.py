"""Split a vehicle's observations into individual trips.

Why this exists
---------------
On weekdays the live feed issues ONE vehicleID per duty (poradie) and keeps it
for the whole shift, while at weekends it issues one per trip. Worse, the
delay it reports is never re-baselined against the new trip's schedule: at
every trip boundary the value jumps by roughly one cycle (+10..16 min on line
37) and then keeps climbing, reaching 900+ minutes by late evening.

Observed on line 37, vehicle 803129320 (2026-07-24):

    06:04 order 2  delay 0     \\  trip 0 — delay behaves normally
    06:24 order 9  delay 9     /   (+9 over 20 min)
    06:26 order 2  delay 22    <-- order RESETS, delay jumps +13
    07:04 order 2  delay 62    <-- +16
    07:34 order 2  delay 91    <-- +15
    21:50 order 21 delay 920

Consequences if untreated: a crude "implausible delay" cut deletes the whole
afternoon (weekday line-37 data stopped dead at 11:00, while weekends covered
04:00-23:00), and the matcher pins a 15-trip duty to a single trip_id.

What this module does
---------------------
`assign_segments` cuts each (service_date, vehicle_id) run wherever
last_stop_order drops — a new trip started — and numbers the pieces. Segment 0
of a duty is the only one whose reported delay carries no accumulated offset,
so `absolute_delay_ok` marks the observations whose *level* can be trusted.
Increment-based analyses (bottlenecks) are unaffected either way: they already
skip the backwards jump at a reset, and within a trip the delay moves
normally.
"""

from __future__ import annotations

import pandas as pd

# A drop of at least this many stops is a genuine new trip rather than the
# feed briefly reporting a lower order (which happens on single observations).
RESET_MIN_DROP = 2


def assign_segments(df: pd.DataFrame) -> pd.DataFrame:
    """Add `segment` (0-based trip index within the run) and
    `absolute_delay_ok` (True where the reported delay level is unbiased).

    A single-segment run — the weekend/normal case — is entirely usable."""
    if df.empty:
        out = df.copy()
        out["segment"] = pd.Series(dtype="int64")
        out["absolute_delay_ok"] = pd.Series(dtype="bool")
        return out

    out = df.sort_values(["service_date", "vehicle_id", "ts"]).copy()
    order = out["last_stop_order"]
    same_run = (
        (out["service_date"] == out["service_date"].shift())
        & (out["vehicle_id"] == out["vehicle_id"].shift())
    )
    # A reset is a meaningful backwards step in stop order within the same run.
    reset = same_run & (order.notna()) & (order.shift().notna()) & (
        order <= order.shift() - RESET_MIN_DROP
    )
    # Restart numbering at each new run.
    out["segment"] = reset.groupby(
        [out["service_date"], out["vehicle_id"]]
    ).cumsum().astype("int64")
    out["absolute_delay_ok"] = out["segment"] == 0
    return out


def segment_key(df: pd.DataFrame) -> pd.Series:
    """Stable per-trip identifier: vehicle_id * 1000 + segment. Lets the
    matcher and the run-level analyses treat each trip as its own run without
    a schema change."""
    return df["vehicle_id"].astype("int64") * 1000 + df["segment"].astype("int64")


def summarize(df: pd.DataFrame) -> dict:
    """Counts for the report: how much of the data is offset-affected."""
    if df.empty:
        return {}
    runs = df.groupby(["service_date", "vehicle_id"])["segment"].max()
    return {
        "runs": int(len(runs)),
        "multi_trip_runs": int((runs > 0).sum()),
        "trips": int(len(df.groupby(["service_date", "vehicle_id", "segment"]))),
        "obs_absolute_ok": int(df["absolute_delay_ok"].sum()),
        "obs_offset_affected": int((~df["absolute_delay_ok"]).sum()),
    }
