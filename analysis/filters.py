"""Load observations into pandas and filter dirty data — at analysis time
only; the raw log in SQLite is never mutated.

Known dirt in the feed:
- parked/finished vehicles hang around with stale trips (e.g. trams on
  line 1 showing 500+ min delay, recurring bad values on line 39),
- whole-minute quantisation makes single readings noisy (fine in aggregate).

Filters applied by `clean()`:
1. implausible delays (outside [MIN, MAX] plausible bounds),
2. stale runs: a vehicle_id whose position barely moves and whose
   last_stop_order never advances over a long stretch is parked, not driving.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

import config
from analysis import segments

# A run is considered stale/parked when it spans at least this long...
STALE_MIN_SPAN_S = 30 * 60
# ...while moving less than this far in total...
STALE_MAX_DISPLACEMENT_M = 150.0
# ...and never advancing along the route.
STALE_MIN_OBS = 5

# Second, independent stale test — "dead trip" detection.
#
# When a vehicle finishes but keeps its trip attached, scheduled progress
# stops while the clock keeps running, so the reported delay grows by ~1
# minute per minute of wall time. A genuinely delayed bus does not behave
# like that for long: it recovers, or its delay plateaus. Runs whose delay
# tracks wall-clock this closely are stale regardless of whether they drift
# a few hundred metres (GPS noise, terminus repositioning), which is what the
# displacement test above misses.
DEAD_TRIP_MIN_SPAN_S = 20 * 60
DEAD_TRIP_MIN_OBS = 6
DEAD_TRIP_MIN_SLOPE = 0.8      # min of delay gained per min elapsed
DEAD_TRIP_MIN_DELAY = 20       # only applies once the delay is already large


def load_observations(conn: sqlite3.Connection,
                      since: str | None = None,
                      until: str | None = None,
                      line: str | None = None) -> pd.DataFrame:
    """Raw observations with parsed timestamps (UTC + local) and derived
    service_date / hour / weekday columns."""
    query = "SELECT * FROM observations WHERE 1=1"
    params: list = []
    if since:
        query += " AND ts >= ?"
        params.append(since)
    if until:
        query += " AND ts <= ?"
        params.append(until)
    if line:
        query += " AND line = ?"
        params.append(line)
    df = pd.read_sql_query(query, conn, params=params)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    local = df["ts"].dt.tz_convert(str(config.LOCAL_TZ))
    df["ts_local"] = local
    df["service_date"] = local.dt.date.astype(str)
    df["hour"] = local.dt.hour
    df["weekday"] = local.dt.dayofweek  # 0 = Monday
    return df


def _run_keys(df: pd.DataFrame) -> list:
    """Group observations by trip when segments are known, else by run.

    Grouping per trip matters: a normal multi-trip weekday duty must not be
    judged as one long frozen run just because its reported delay climbs
    across trip boundaries."""
    keys = [df["service_date"], df["vehicle_id"]]
    if "segment" in df.columns:
        keys.append(df["segment"])
    return keys


def clean_direction(destination: pd.Series) -> pd.Series:
    """Derive a clean direction label from the live destination string.

    The live feed always carries a destination (e.g. "Bratislava, Most SNP"),
    so it is a far more reliable grouping key than the matched GTFS
    direction_id, which is only present for runs that matched. Strips the
    "Bratislava, " prefix; empty destinations become "?".
    """
    dest = (destination.fillna("")
            .str.replace(r"^\s*Bratislava,\s*", "", regex=True)
            .str.strip())
    return dest.where(dest != "", "?")


def _haversine_m(lat1, lng1, lat2, lng2):
    r = 6_371_000.0
    lat1, lng1, lat2, lng2 = map(np.radians, (lat1, lng1, lat2, lng2))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lng2 - lng1) / 2) ** 2)
    return 2 * r * np.arcsin(np.sqrt(a))


def flag_stale_runs(df: pd.DataFrame) -> pd.Series:
    """Boolean Series (aligned to df.index): True for every observation of a
    run judged stale/parked for that whole service date."""
    stale = pd.Series(False, index=df.index)
    for _, group in df.groupby(_run_keys(df)):
        if len(group) < STALE_MIN_OBS:
            continue
        span = (group["ts"].max() - group["ts"].min()).total_seconds()
        if span < STALE_MIN_SPAN_S:
            continue
        orders = group["last_stop_order"].dropna()
        if not orders.empty and orders.nunique() > 1:
            continue  # it advanced along the route -> genuinely driving
        displacement = _haversine_m(
            group["lat"].min(), group["lng"].min(),
            group["lat"].max(), group["lng"].max(),
        )
        if displacement < STALE_MAX_DISPLACEMENT_M:
            stale.loc[group.index] = True
    return stale


def flag_dead_trips(df: pd.DataFrame) -> pd.Series:
    """Boolean Series: True for observations of runs whose reported delay
    grows about as fast as wall-clock time — a finished vehicle still
    carrying its trip. Catches the stale runs that drift too far for the
    displacement test (which is why 60-90 min junk was surviving)."""
    dead = pd.Series(False, index=df.index)
    for _, group in df.groupby(_run_keys(df)):
        if len(group) < DEAD_TRIP_MIN_OBS:
            continue
        group = group.sort_values("ts")
        elapsed = (group["ts"] - group["ts"].iloc[0]).dt.total_seconds() / 60.0
        delay = group["delay_minutes"].astype(float)
        if elapsed.iloc[-1] * 60 < DEAD_TRIP_MIN_SPAN_S:
            continue
        if delay.max() < DEAD_TRIP_MIN_DELAY:
            continue
        if elapsed.var() == 0:
            continue
        # least-squares slope of delay vs elapsed minutes
        slope = elapsed.cov(delay) / elapsed.var()
        if slope >= DEAD_TRIP_MIN_SLOPE:
            dead.loc[group.index] = True
    return dead


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Apply all dirty-data filters; returns a copy with a report attached
    in df.attrs['filter_report'].

    Order matters. Segmentation runs FIRST, on complete runs, so trip
    boundaries are detected before anything is removed. The absolute-delay cut
    is then applied only where the reported level is meaningful — i.e. the
    first trip of a duty. Later trips carry an accumulated schedule offset
    (see analysis/segments.py), so cutting them on absolute value deleted
    every weekday afternoon; they are kept, flagged, and used only by
    increment-based analyses."""
    if df.empty:
        df.attrs["filter_report"] = {}
        return df
    n0 = len(df)

    df = segments.assign_segments(df)
    seg_stats = segments.summarize(df)

    # Stale/parked and frozen-trip tests operate per trip, so a normal
    # multi-trip duty is no longer mistaken for one runaway run.
    stale = flag_stale_runs(df)
    df = df[~stale]
    n1 = len(df)
    dead = flag_dead_trips(df)
    df = df[~dead]
    n2 = len(df)

    implausible = (
        df["absolute_delay_ok"]
        & df["delay_minutes"].notna()
        & ~df["delay_minutes"].between(
            config.MIN_PLAUSIBLE_DELAY_MIN, config.MAX_PLAUSIBLE_DELAY_MIN
        )
    )
    df = df[~implausible].copy()

    report = {
        "raw": n0,
        "dropped_stale_parked": n0 - n1,
        "dropped_dead_trip": n1 - n2,
        "dropped_implausible_delay": n2 - len(df),
        "kept": len(df),
    }
    report.update(seg_stats)
    df.attrs["filter_report"] = report
    return df
