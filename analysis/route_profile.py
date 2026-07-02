"""Analysis 3: WHERE on the route delays appear.

For each run, take consecutive observations where last_stop_order advanced
and attribute the delay change to that inter-stop segment. Averaged over
many runs, segments with a consistently positive increment are the
bottlenecks; the GPS midpoint localises each to a spot (junction / light).

Granularity caveat: positions arrive ~every 2 minutes, so a fast vehicle
can skip several stops between observations — the increment is then spread
over a multi-stop segment (span > 1). That is inherent to the source.
"""

from __future__ import annotations

import pandas as pd


def segment_increments(df: pd.DataFrame) -> pd.DataFrame:
    """One row per observed segment traversal:
    (line, direction, from_order, to_order, delay_increment, mid lat/lng)."""
    if df.empty:
        return pd.DataFrame()
    df = df.dropna(subset=["delay_minutes", "last_stop_order"]).copy()
    if "direction_id" in df.columns:
        df["direction"] = df["direction_id"].fillna(
            "dest:" + df["destination"].fillna("?"))
    else:
        df["direction"] = "dest:" + df["destination"].fillna("?")

    rows = []
    for (service_date, vehicle_id), run in df.groupby(["service_date", "vehicle_id"]):
        run = run.sort_values("ts")
        prev = None
        for _, obs in run.iterrows():
            if prev is not None and obs["last_stop_order"] > prev["last_stop_order"]:
                rows.append({
                    "line": obs["line"],
                    "direction": obs["direction"],
                    "from_order": int(prev["last_stop_order"]),
                    "to_order": int(obs["last_stop_order"]),
                    "span": int(obs["last_stop_order"] - prev["last_stop_order"]),
                    "delay_increment": obs["delay_minutes"] - prev["delay_minutes"],
                    "mid_lat": (obs["lat"] + prev["lat"]) / 2,
                    "mid_lng": (obs["lng"] + prev["lng"]) / 2,
                    "hour": obs["hour"],
                    "service_date": service_date,
                    "vehicle_id": vehicle_id,
                })
            prev = obs
    return pd.DataFrame(rows)


def bottleneck_table(increments: pd.DataFrame,
                     min_n: int = 10) -> pd.DataFrame:
    """Rank segments by mean delay increment (per stop traversed, so
    multi-stop spans don't dominate)."""
    if increments.empty:
        return increments
    inc = increments.copy()
    inc["increment_per_stop"] = inc["delay_increment"] / inc["span"]
    grouped = inc.groupby(["line", "direction", "from_order", "to_order"])
    table = grouped.agg(
        n=("delay_increment", "count"),
        mean_increment=("increment_per_stop", "mean"),
        total_mean=("delay_increment", "mean"),
        mid_lat=("mid_lat", "mean"),
        mid_lng=("mid_lng", "mean"),
    ).reset_index()
    table = table[table["n"] >= min_n]
    return table.sort_values("mean_increment", ascending=False).round(3)


def bottleneck_map(table: pd.DataFrame, out_path: str) -> str | None:
    """Optional folium heatmap of segment delay gain. Returns the output
    path, or None when folium isn't installed or there's nothing to plot."""
    try:
        import folium
    except ImportError:
        return None
    if table.empty:
        return None
    gains = table[table["mean_increment"] > 0]
    if gains.empty:
        return None
    m = folium.Map(
        location=[gains["mid_lat"].mean(), gains["mid_lng"].mean()],
        zoom_start=12, tiles="cartodbpositron",
    )
    max_gain = gains["mean_increment"].max()
    for _, seg in gains.iterrows():
        folium.CircleMarker(
            location=[seg["mid_lat"], seg["mid_lng"]],
            radius=4 + 16 * seg["mean_increment"] / max_gain,
            color="#C4553F", fill=True, fill_opacity=0.6, weight=1,
            tooltip=(f"{seg['line']} {seg['direction']} "
                     f"stops {seg['from_order']}→{seg['to_order']}: "
                     f"+{seg['mean_increment']:.2f} min/stop (n={seg['n']})"),
        ).add_to(m)
    m.save(out_path)
    return out_path
