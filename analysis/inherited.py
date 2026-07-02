"""Analysis 4: inherited ("prenesené") vs gained ("získané") delay.

A vehicle observed already late near the origin cannot have gained that
delay on a route it has not yet driven — that part is inherited from its
previous circuit (or a schedule too tight to recover in the layover).
Delay growth after the origin is gained on route, in traffic.

Two views:
1. per-run split: inherited = delay at the first near-origin observation,
   gained = final delay - inherited;
2. circuit chaining by (service_date, line, poradie): compare the delay at
   the END of run k with the INHERITED delay at the start of run k+1 on the
   same poradie -> how much the terminus layover absorbs.

This is also where the early-morning systemic signal shows up: a uniform
~6 min delay at ~06:00 with empty roads = inherited/schedule-tight, not
congestion. `morning_signal()` checks it across days.
"""

from __future__ import annotations

import pandas as pd

# An observation counts as "near the origin" while the vehicle has passed
# at most this many stops.
ORIGIN_MAX_STOP_ORDER = 2


def run_split(df: pd.DataFrame) -> pd.DataFrame:
    """One row per run: inherited, final, gained delay (+ context)."""
    if df.empty:
        return pd.DataFrame()
    df = df.dropna(subset=["delay_minutes"]).copy()
    rows = []
    for (service_date, vehicle_id), run in df.groupby(["service_date", "vehicle_id"]):
        run = run.sort_values("ts")
        near_origin = run[run["last_stop_order"] <= ORIGIN_MAX_STOP_ORDER]
        if near_origin.empty:
            continue  # never seen near the origin -> can't split honestly
        inherited = near_origin.iloc[0]["delay_minutes"]
        final = run.iloc[-1]["delay_minutes"]
        rows.append({
            "service_date": service_date,
            "vehicle_id": vehicle_id,
            "line": run.iloc[0]["line"],
            "destination": run.iloc[0]["destination"],
            "poradie": run.iloc[0].get("poradie"),
            "first_ts_local": run.iloc[0]["ts_local"],
            "start_hour": run.iloc[0]["hour"],
            "inherited": inherited,
            "final": final,
            "gained": final - inherited,
            "n_obs": len(run),
        })
    return pd.DataFrame(rows)


def chain_circuits(splits: pd.DataFrame) -> pd.DataFrame:
    """Chain consecutive runs of the same (service_date, line, poradie):
    end-of-run delay vs next run's inherited delay = layover absorption."""
    if splits.empty or "poradie" not in splits.columns:
        return pd.DataFrame()
    chained = splits.dropna(subset=["poradie"]).sort_values("first_ts_local")
    rows = []
    for (service_date, line, poradie), circ in chained.groupby(
            ["service_date", "line", "poradie"]):
        prev = None
        for _, run in circ.iterrows():
            if prev is not None:
                rows.append({
                    "service_date": service_date,
                    "line": line,
                    "poradie": poradie,
                    "prev_final": prev["final"],
                    "next_inherited": run["inherited"],
                    "layover_absorbed": prev["final"] - run["inherited"],
                })
            prev = run
    return pd.DataFrame(rows)


def morning_signal(splits: pd.DataFrame,
                   hour_from: int = 5, hour_to: int = 7) -> pd.DataFrame:
    """Early-morning inherited delay per day: if runs start already ~6 min
    late at 06:00 with empty roads, the timetable (or overnight chaining) is
    the culprit, not traffic."""
    if splits.empty:
        return pd.DataFrame()
    morning = splits[splits["start_hour"].between(hour_from, hour_to)]
    if morning.empty:
        return pd.DataFrame()
    return morning.groupby("service_date").agg(
        runs=("inherited", "count"),
        median_inherited=("inherited", "median"),
        median_gained=("gained", "median"),
    ).round(2).reset_index()
