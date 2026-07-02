"""Analysis 1: punctuality distribution per line x direction x hour x weekday.

Distribution, not just an average: median, P10/P90 spread, % on time within
the configured threshold, and observation counts (so thin cells are visible).
Note: delay_minutes is whole-minute quantised — good for aggregates, noisy
for any single trip.
"""

from __future__ import annotations

import pandas as pd

import config


def _direction(df: pd.DataFrame) -> pd.Series:
    """Prefer the matched GTFS direction_id; fall back to the live
    destination string so unmatched runs still group sensibly."""
    if "direction_id" in df.columns:
        return df["direction_id"].fillna("dest:" + df["destination"].fillna("?"))
    return "dest:" + df["destination"].fillna("?")


def attach_matches(df: pd.DataFrame, conn) -> pd.DataFrame:
    """Left-join matched_runs onto observations by (service_date, vehicle_id)."""
    matches = pd.read_sql_query(
        "SELECT service_date, vehicle_id, trip_id, direction_id, poradie"
        " FROM matched_runs",
        conn,
    )
    if matches.empty:
        for col in ("trip_id", "direction_id", "poradie"):
            df[col] = None
        return df
    return df.merge(matches, on=["service_date", "vehicle_id"], how="left")


def punctuality_table(df: pd.DataFrame,
                      by_weekday: bool = True) -> pd.DataFrame:
    """Aggregate delay distribution per (line, direction[, weekday], hour)."""
    if df.empty:
        return pd.DataFrame()
    df = df.dropna(subset=["delay_minutes"]).copy()
    df["direction"] = _direction(df)
    keys = ["line", "direction"] + (["weekday"] if by_weekday else []) + ["hour"]

    threshold = config.ON_TIME_THRESHOLD_MIN
    grouped = df.groupby(keys)["delay_minutes"]
    table = grouped.agg(
        n="count",
        median="median",
        mean="mean",
        p10=lambda s: s.quantile(0.10),
        p90=lambda s: s.quantile(0.90),
        max="max",
        pct_on_time=lambda s: 100.0 * (s <= threshold).mean(),
    ).reset_index()
    return table.round(2)


def worst_cells(table: pd.DataFrame, min_n: int = 20, top: int = 15) -> pd.DataFrame:
    """The bad-day tail: cells with enough data, ranked by P90 delay."""
    if table.empty:
        return table
    solid = table[table["n"] >= min_n]
    return solid.sort_values("p90", ascending=False).head(top)
