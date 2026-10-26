"""Analysis 3: WHERE on the route delays appear.

For each run, take consecutive observations where last_stop_order advanced
and attribute the delay change to that inter-stop segment. Averaged over
many runs, segments with a consistently positive increment are the
bottlenecks; the GPS midpoint localises each to a spot (junction / light).

Granularity caveat: positions arrive ~every 2 minutes, so a fast vehicle
can skip several stops between observations — the increment is then spread
over a multi-stop segment (span > 1). That is inherent to the source.

Ranking caveat, and why there are two rankings: minutes-per-STOP flatters
long inter-stop gaps. A segment that covers 2 km of open road is expected to
cost more delay than one crossing 200 m of housing estate, so a high
min/stop can mean "this is slow" or merely "these two stops are far apart".
Minutes-per-KM separates the two, and a segment that ranks high on both is a
genuine bottleneck rather than an artefact of stop spacing.
"""

from __future__ import annotations

import pandas as pd

from analysis import filters

# Below this the straight-line distance between two fixes is comparable to GPS
# noise, so min/km explodes on a rounding error. Such traversals still count
# towards min/stop; they are simply excluded from the per-km statistic.
MIN_SEGMENT_KM = 0.05

# A traversal covering more stops than this is a SAMPLING GAP, not a segment.
# The ~2-minute fix interval means a vehicle can cover a dozen stops between
# reports; grouping those under one (from_order, to_order) key invents a
# "segment" that spans a third of the route and that no rider experiences as
# one place. Worse, they break the per-km denominator: straight-line distance
# between two fixes eleven stops apart is meaningless on a route that curves
# or doubles back, so a long hop can end up close to where it started and
# divide a real delay by a few hundred metres.
#
# Line 37 real case (Sep 2026): Most SNP 11->22, n=11, -0.38 min/stop but
# +3.195 min/km — a segment losing time per stop and apparently gaining it per
# kilometre. Both causes are fixed here: spans this long are excluded, and
# per-km is a ratio of totals rather than a mean of per-row ratios.
MAX_SEGMENT_SPAN = 3


def segment_increments(df: pd.DataFrame) -> pd.DataFrame:
    """One row per observed segment traversal:
    (line, direction, from_order, to_order, delay_increment, mid lat/lng,
    distance_km).

    distance_km is the straight-line distance between the two fixes, so it
    UNDER-states the road distance actually driven (more so on a bend). It is
    a normaliser for comparing segments against each other, not a measurement
    of route length."""
    if df.empty:
        return pd.DataFrame()
    df = df.dropna(subset=["delay_minutes", "last_stop_order"]).copy()
    df["direction"] = filters.clean_direction(df["destination"])

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
                    "distance_km": filters.haversine_m(
                        prev["lat"], prev["lng"], obs["lat"], obs["lng"]) / 1000.0,
                    "hour": obs["hour"],
                    "service_date": service_date,
                    "vehicle_id": vehicle_id,
                })
            prev = obs
    return pd.DataFrame(rows)


def bottleneck_table(increments: pd.DataFrame, min_n: int = 10,
                     max_span: int = MAX_SEGMENT_SPAN) -> pd.DataFrame:
    """Rank segments by mean delay increment per stop traversed (so multi-stop
    spans don't dominate), alongside the same loss normalised by DISTANCE.

    `mean_increment` (min/stop) is the headline ranking. A segment can top it
    simply by spanning more ground, so the table also carries `km_per_stop` —
    how far apart these stops actually are — and `increment_per_km`. Read them
    together: high min/stop with ordinary km_per_stop is a real bottleneck;
    high min/stop with a large km_per_stop and unremarkable min/km is just a
    long gap between stops.

    Traversals spanning more than `max_span` stops are dropped: they are
    sampling gaps rather than segments, and their straight-line distance is
    not a usable denominator (see MAX_SEGMENT_SPAN).

    `increment_per_km` is a ratio of TOTALS — all delay gained over all
    distance covered — not a mean of per-row ratios. Per-row ratios let a
    single traversal with a small denominator dominate the group and can
    invert its sign against `mean_increment`, which is exactly what the
    Most SNP 11->22 row did."""
    if increments.empty:
        return increments
    inc = increments.copy()
    if "span" in inc.columns and max_span:
        inc = inc[inc["span"] <= max_span]
    if inc.empty:
        return pd.DataFrame()
    inc["increment_per_stop"] = inc["delay_increment"] / inc["span"]
    if "distance_km" not in inc.columns:
        inc["distance_km"] = float("nan")
    inc["km_per_stop_row"] = inc["distance_km"] / inc["span"]
    # Only traversals long enough for the distance to mean something feed the
    # per-km statistic; the rest would divide a real delay by GPS jitter.
    measurable = inc["distance_km"] >= MIN_SEGMENT_KM
    inc["km_measurable"] = inc["distance_km"].where(measurable)
    inc["delay_measurable"] = inc["delay_increment"].where(measurable)

    grouped = inc.groupby(["line", "direction", "from_order", "to_order"])
    table = grouped.agg(
        n=("delay_increment", "count"),
        mean_increment=("increment_per_stop", "mean"),
        total_mean=("delay_increment", "mean"),
        km_per_stop=("km_per_stop_row", "mean"),
        _delay_total=("delay_measurable", "sum"),
        _km_total=("km_measurable", "sum"),
        n_km=("km_measurable", "count"),
        mid_lat=("mid_lat", "mean"),
        mid_lng=("mid_lng", "mean"),
    ).reset_index()
    table["increment_per_km"] = (
        table["_delay_total"] / table["_km_total"]).where(table["n_km"] > 0)
    table = table.drop(columns=["_delay_total", "_km_total"])
    table = table[table["n"] >= min_n]
    return table.sort_values("mean_increment", ascending=False).round(3)


def by_distance(table: pd.DataFrame, min_n: int = 10,
                top: int = 15) -> pd.DataFrame:
    """The same segments re-ranked by delay gained per KILOMETRE.

    Segments appearing near the top of both this and `bottleneck_table` are
    the ones worth acting on. A segment that only tops the min/stop ranking is
    explained by stop spacing, not by anything happening on the road."""
    if table is None or table.empty or "increment_per_km" not in table.columns:
        return pd.DataFrame()
    solid = table[(table.get("n_km", 0) >= min_n)
                  & table["increment_per_km"].notna()]
    if solid.empty:
        return pd.DataFrame()
    return solid.sort_values("increment_per_km", ascending=False).head(top)


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
