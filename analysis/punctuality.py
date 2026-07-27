"""Analysis 1: punctuality distribution per line x direction x hour x weekday.

Distribution, not just an average: median, P10/P90 spread, % on time within
the configured threshold, and observation counts (so thin cells are visible).
Note: delay_minutes is whole-minute quantised — good for aggregates, noisy
for any single trip.
"""

from __future__ import annotations

import pandas as pd

import config
from analysis.filters import clean_direction


def _direction(df: pd.DataFrame) -> pd.Series:
    """Group by the clean live destination (always present) rather than the
    sparse matched direction_id, so a line isn't split into "0" / "dest:X"
    pseudo-directions depending on which runs happened to match."""
    return clean_direction(df["destination"])


def attach_matches(df: pd.DataFrame, conn) -> pd.DataFrame:
    """Left-join matched_runs onto observations by (service_date, vehicle_id)."""
    matches = pd.read_sql_query(
        "SELECT service_date, vehicle_id, segment, trip_id, direction_id, poradie"
        " FROM matched_runs",
        conn,
    )
    if matches.empty:
        for col in ("trip_id", "direction_id", "poradie"):
            df[col] = None
        return df
    # Join per TRIP when the observations carry segments; otherwise fall back
    # to the run-level key so the function still works on unsegmented input.
    keys = ["service_date", "vehicle_id"]
    if "segment" in df.columns:
        keys.append("segment")
    else:
        matches = matches[matches["segment"] == 0]
    return df.merge(matches.drop(columns=[c for c in ("segment",) if c not in keys]),
                    on=keys, how="left")


def punctuality_table(df: pd.DataFrame,
                      by_weekday: bool = True) -> pd.DataFrame:
    """Aggregate delay distribution per (line, direction[, weekday], hour).

    Uses only observations whose delay LEVEL is unbiased (the first trip of a
    duty). Later trips of a multi-trip duty carry an accumulated schedule
    offset and would inflate every statistic here — see analysis/segments.py."""
    if df.empty:
        return pd.DataFrame()
    if "absolute_delay_ok" in df.columns:
        df = df[df["absolute_delay_ok"]]
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
