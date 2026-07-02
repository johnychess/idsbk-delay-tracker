"""Analysis 6: per-physical-vehicle reliability.

The live feed carries no physical identity (licenseNumber is ~always null),
so this joins through the imhd.sk výprava table:

    matched run -> poradie (decoded from GTFS trip_id)
                -> (date, line, poradie) -> evidenčné číslo

Honesty notes baked into the output:
- výprava is daily-granular and "recorded automatically, not yet verified";
- when several vehicles served the same poradie during a day (swaps,
  a/b sub-runs), the run is marked ambiguous rather than guessed.
"""

from __future__ import annotations

import sqlite3

import pandas as pd

from gtfs.poradie import normalize_poradie


def vehicle_for_runs(conn: sqlite3.Connection) -> pd.DataFrame:
    """matched_runs enriched with the physical vehicle where unambiguous."""
    runs = pd.read_sql_query(
        "SELECT service_date, vehicle_id, line, trip_id, direction_id, poradie"
        " FROM matched_runs WHERE trip_id IS NOT NULL AND poradie IS NOT NULL",
        conn,
    )
    if runs.empty:
        return runs
    vyp = pd.read_sql_query("SELECT date, line, poradie, vehicle FROM vyprava", conn)
    if vyp.empty:
        runs["physical_vehicle"] = None
        runs["assignment"] = "no_vyprava_data"
        return runs

    # Normalize both sides: GTFS gives "03", výprava gives "3" (or "2a" for
    # sub-runs — those match their base poradie as candidates too).
    vyp = vyp.copy()
    vyp["poradie_norm"] = vyp["poradie"].map(normalize_poradie)
    vyp["poradie_base"] = vyp["poradie_norm"].str.rstrip("abcdefgh")

    grouped = vyp.groupby(["date", "line", "poradie_base"])["vehicle"].agg(
        lambda v: sorted(set(v)))

    vehicles, assignment = [], []
    for _, run in runs.iterrows():
        base = (run["poradie"] or "").rstrip("abcdefgh")
        candidates = grouped.get((run["service_date"], run["line"], base), [])
        if len(candidates) == 1:
            vehicles.append(candidates[0])
            assignment.append("unique")
        elif len(candidates) > 1:
            vehicles.append(",".join(candidates))
            assignment.append("ambiguous")  # swap / sub-runs during the day
        else:
            vehicles.append(None)
            assignment.append("not_in_vyprava")
    runs["physical_vehicle"] = vehicles
    runs["assignment"] = assignment
    return runs


def vehicle_reliability(conn: sqlite3.Connection,
                        observations: pd.DataFrame,
                        min_runs: int = 3) -> pd.DataFrame:
    """Delay stats per evidenčné číslo, over unambiguously attributed runs."""
    attributed = vehicle_for_runs(conn)
    if attributed.empty:
        return pd.DataFrame()
    attributed = attributed[attributed["assignment"] == "unique"]
    if attributed.empty or observations.empty:
        return pd.DataFrame()

    merged = observations.merge(
        attributed[["service_date", "vehicle_id", "physical_vehicle"]],
        on=["service_date", "vehicle_id"],
        how="inner",
        suffixes=("", "_attr"),
    ).dropna(subset=["delay_minutes"])
    if merged.empty:
        return pd.DataFrame()

    per_run = merged.groupby(
        ["physical_vehicle", "service_date", "vehicle_id"]
    )["delay_minutes"].median().reset_index()

    table = per_run.groupby("physical_vehicle").agg(
        runs=("vehicle_id", "count"),
        median_delay=("delay_minutes", "median"),
        mean_delay=("delay_minutes", "mean"),
        worst_run=("delay_minutes", "max"),
    ).reset_index()
    table = table[table["runs"] >= min_runs]
    return table.sort_values("median_delay", ascending=False).round(2)
