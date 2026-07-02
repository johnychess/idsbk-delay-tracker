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

# A run is considered stale/parked when it spans at least this long...
STALE_MIN_SPAN_S = 30 * 60
# ...while moving less than this far in total...
STALE_MAX_DISPLACEMENT_M = 150.0
# ...and never advancing along the route.
STALE_MIN_OBS = 5


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
    for (_, _), group in df.groupby(["service_date", "vehicle_id"]):
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


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Apply all dirty-data filters; returns a copy with a report attached
    in df.attrs['filter_report']."""
    if df.empty:
        df.attrs["filter_report"] = {}
        return df
    n0 = len(df)
    plausible = df["delay_minutes"].between(
        config.MIN_PLAUSIBLE_DELAY_MIN, config.MAX_PLAUSIBLE_DELAY_MIN
    ) | df["delay_minutes"].isna()
    df = df[plausible]
    n1 = len(df)
    stale = flag_stale_runs(df)
    df = df[~stale].copy()
    df.attrs["filter_report"] = {
        "raw": n0,
        "dropped_implausible_delay": n0 - n1,
        "dropped_stale_parked": n1 - len(df),
        "kept": len(df),
    }
    return df
