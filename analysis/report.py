"""Run all six analyses over the accumulated DB and write a report:
a markdown summary + PNG plots (+ optional folium bottleneck map).

    python -m analysis.report --line 37 [--db data/tracker.sqlite]
                              [--since 2026-07-01] [--until 2026-07-08]
                              [--out reports]

Prerequisite: run the matcher for the dates of interest first
(python -m match.matcher --date YYYY-MM-DD), otherwise the poradie- and
vehicle-based analyses have nothing to join on.
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import date

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap

import config
import storage
from analysis import filters, inherited, missed, punctuality, route_profile, vehicles

log = logging.getLogger(__name__)

# Validated reference palette (dataviz skill): categorical slots in fixed
# order, status colors reserved for verdict states, recessive chrome.
PALETTE = {
    "series1": "#2a78d6",   # blue
    "series2": "#1baf7a",   # aqua (sub-3:1 on light surface -> direct labels)
    "critical": "#d03b3b",  # status: missed departures
    "surface": "#fcfcfb",
    "ink": "#0b0b0b",
    "ink2": "#52514e",
    "muted": "#898781",
    "grid": "#e1e0d9",
    "baseline": "#c3c2b7",
}
SEQUENTIAL = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
SEQ_CMAP = LinearSegmentedColormap.from_list("seq_blue", SEQUENTIAL)

plt.rcParams.update({
    "figure.facecolor": PALETTE["surface"],
    "axes.facecolor": PALETTE["surface"],
    "savefig.facecolor": PALETTE["surface"],
    "text.color": PALETTE["ink"],
    "axes.labelcolor": PALETTE["ink2"],
    "xtick.color": PALETTE["muted"],
    "ytick.color": PALETTE["muted"],
    "axes.edgecolor": PALETTE["baseline"],
    "axes.grid": True,
    "grid.color": PALETTE["grid"],
    "grid.linewidth": 0.6,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.spines.left": False,
    "font.family": "sans-serif",
    "figure.dpi": 150,
})


def _finish(ax, title: str, ylabel: str) -> None:
    ax.set_title(title, loc="left", fontsize=11, color=PALETTE["ink"], pad=10)
    ax.set_ylabel(ylabel)
    ax.set_axisbelow(True)


def plot_delay_by_hour(df: pd.DataFrame, out_path: str) -> bool:
    """Median delay per hour, one line per direction, with a P10-P90 band."""
    data = df.dropna(subset=["delay_minutes"]).copy()
    if data.empty:
        return False
    data["direction"] = data["direction_id"].fillna(
        "dest:" + data["destination"].fillna("?"))
    fig, ax = plt.subplots(figsize=(8, 4.2))
    colors = [PALETTE["series1"], PALETTE["series2"]]
    for i, (direction, grp) in enumerate(list(data.groupby("direction"))[:2]):
        stats = grp.groupby("hour")["delay_minutes"].agg(
            median="median",
            p10=lambda s: s.quantile(0.10),
            p90=lambda s: s.quantile(0.90),
        )
        color = colors[i % len(colors)]
        ax.fill_between(stats.index, stats["p10"], stats["p90"],
                        color=color, alpha=0.12, linewidth=0)
        ax.plot(stats.index, stats["median"], color=color, linewidth=2,
                label=f"direction {direction}")
        ax.annotate(f"dir {direction}", xy=(stats.index[-1], stats["median"].iloc[-1]),
                    xytext=(6, 0), textcoords="offset points",
                    color=color, fontsize=9, va="center")
    ax.set_xlabel("hour of day (local)")
    ax.set_xticks(range(4, 25, 2))
    ax.legend(frameon=False, fontsize=9)
    _finish(ax, "Delay by hour of day — median line, P10–P90 band", "delay (min)")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return True


def plot_weekday_hour_heatmap(df: pd.DataFrame, out_path: str) -> bool:
    data = df.dropna(subset=["delay_minutes"])
    if data.empty:
        return False
    pivot = data.pivot_table(index="weekday", columns="hour",
                             values="delay_minutes", aggfunc="median")
    fig, ax = plt.subplots(figsize=(8, 3.4))
    mesh = ax.pcolormesh(pivot.columns, pivot.index, pivot.values,
                         cmap=SEQ_CMAP, edgecolors=PALETTE["surface"], linewidth=2)
    ax.set_yticks(range(7))
    ax.set_yticklabels(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
    ax.invert_yaxis()
    ax.grid(False)
    ax.set_xlabel("hour of day (local)")
    fig.colorbar(mesh, ax=ax, label="median delay (min)", shrink=0.85)
    _finish(ax, "Median delay by weekday × hour", "")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return True


def plot_inherited_gained(splits: pd.DataFrame, out_path: str) -> bool:
    if splits.empty:
        return False
    by_hour = splits.groupby("start_hour")[["inherited", "gained"]].median()
    fig, ax = plt.subplots(figsize=(8, 4))
    x = by_hour.index.to_numpy()
    width = 0.38
    ax.bar(x - width / 2, by_hour["inherited"], width, label="inherited",
           color=PALETTE["series1"], edgecolor=PALETTE["surface"], linewidth=1)
    ax.bar(x + width / 2, by_hour["gained"], width, label="gained on route",
           color=PALETTE["series2"], edgecolor=PALETTE["surface"], linewidth=1)
    ax.axhline(0, color=PALETTE["baseline"], linewidth=1)
    ax.set_xlabel("run start hour (local)")
    ax.legend(frameon=False, fontsize=9)
    _finish(ax, "Inherited vs gained delay by start hour (median per run)",
            "delay (min)")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return True


def plot_missed(missed_by_date: pd.DataFrame, out_path: str) -> bool:
    if missed_by_date.empty:
        return False
    counts = (missed_by_date.groupby(["service_date", "verdict"]).size()
              .unstack(fill_value=0))
    for col in ("missed", "unknown"):
        if col not in counts:
            counts[col] = 0
    fig, ax = plt.subplots(figsize=(8, 3.6))
    x = range(len(counts.index))
    width = 0.38
    ax.bar([i - width / 2 for i in x], counts["missed"], width,
           label="missed", color=PALETTE["critical"],
           edgecolor=PALETTE["surface"], linewidth=1)
    ax.bar([i + width / 2 for i in x], counts["unknown"], width,
           label="unknown (no coverage)", color=PALETTE["muted"],
           edgecolor=PALETTE["surface"], linewidth=1)
    for i, (miss, unk) in enumerate(zip(counts["missed"], counts["unknown"])):
        if miss:
            ax.annotate(str(miss), xy=(i - width / 2, miss), xytext=(0, 3),
                        textcoords="offset points", ha="center", fontsize=8,
                        color=PALETTE["ink2"])
    ax.set_xticks(list(x))
    ax.set_xticklabels(counts.index, rotation=45, ha="right", fontsize=8)
    ax.yaxis.get_major_locator().set_params(integer=True)
    ax.legend(frameon=False, fontsize=9)
    _finish(ax, "Missed scheduled departures per day (conservative)", "departures")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return True


def _md_table(df: pd.DataFrame, max_rows: int = 20) -> str:
    if df is None or df.empty:
        return "_no data_\n"
    return df.head(max_rows).to_markdown(index=False) + "\n"


def build_report(db_path: str, line: str, since: str | None, until: str | None,
                 out_dir: str) -> str:
    conn = storage.connect(db_path)
    os.makedirs(out_dir, exist_ok=True)

    raw = filters.load_observations(conn, since=since, until=until, line=line)
    df = filters.clean(raw)
    freport = df.attrs.get("filter_report", {})
    if not df.empty:
        df = punctuality.attach_matches(df, conn)

    sections: list[str] = [
        f"# Punctuality report — line {line}",
        f"_Data: {freport.get('kept', 0)} observations kept "
        f"({freport.get('dropped_implausible_delay', 0)} implausible-delay and "
        f"{freport.get('dropped_stale_parked', 0)} stale/parked rows filtered "
        f"out of {freport.get('raw', 0)} raw)._\n",
    ]

    if df.empty:
        sections.append("**No observations in the selected window.** "
                        "Let the collector run first.")
    else:
        dates = sorted(df["service_date"].unique())
        sections.append(f"_Window: {dates[0]} … {dates[-1]} "
                        f"({len(dates)} service days)._\n")

        # 1+2: punctuality distribution and its time structure
        table = punctuality.punctuality_table(df, by_weekday=False)
        sections.append("## 1. Punctuality by direction × hour\n")
        sections.append(f"On-time threshold: ≤ {config.ON_TIME_THRESHOLD_MIN} min. "
                        "Delays are whole-minute quantised — read aggregates, "
                        "not single trips.\n")
        sections.append(_md_table(table, max_rows=48))
        worst = punctuality.worst_cells(punctuality.punctuality_table(df))
        sections.append("### Worst cells (P90, n ≥ 20)\n")
        sections.append(_md_table(worst))
        if plot_delay_by_hour(df, os.path.join(out_dir, "delay_by_hour.png")):
            sections.append("![delay by hour](delay_by_hour.png)\n")
        if plot_weekday_hour_heatmap(df, os.path.join(out_dir, "weekday_hour_heatmap.png")):
            sections.append("![weekday × hour](weekday_hour_heatmap.png)\n")

        # 3: where on the route
        increments = route_profile.segment_increments(df)
        bottlenecks = route_profile.bottleneck_table(increments)
        sections.append("## 2. Bottleneck segments (delay gained per stop)\n")
        sections.append(_md_table(bottlenecks))
        map_path = route_profile.bottleneck_map(
            bottlenecks, os.path.join(out_dir, "bottleneck_map.html"))
        if map_path:
            sections.append("Interactive map: `bottleneck_map.html`\n")

        # 4: inherited vs gained
        splits = inherited.run_split(df)
        sections.append("## 3. Inherited vs gained delay\n")
        if not splits.empty:
            sections.append(
                f"Across {len(splits)} runs: median inherited "
                f"**{splits['inherited'].median():.1f} min**, median gained "
                f"**{splits['gained'].median():.1f} min**.\n")
            chains = inherited.chain_circuits(splits)
            if not chains.empty:
                sections.append(
                    f"Circuit chaining ({len(chains)} consecutive-run pairs on "
                    f"the same poradie): median layover absorption "
                    f"**{chains['layover_absorbed'].median():.1f} min** "
                    "(end-of-run delay minus next run's inherited delay).\n")
            morning = inherited.morning_signal(splits)
            if not morning.empty:
                sections.append("### Early-morning systemic signal (05–07 h starts)\n")
                sections.append(
                    "Inherited delay before the roads fill up points at the "
                    "timetable, not traffic:\n")
                sections.append(_md_table(morning))
            if plot_inherited_gained(splits, os.path.join(out_dir, "inherited_vs_gained.png")):
                sections.append("![inherited vs gained](inherited_vs_gained.png)\n")
        else:
            sections.append("_No runs observed near their origin — cannot split "
                            "honestly. (Needs matched runs and origin coverage.)_\n")

        # 5: missed departures
        sections.append("## 4. Missed departures (conservative)\n")
        all_verdicts = []
        for d in dates:
            verdicts = missed.missed_departures(conn, date.fromisoformat(d), line)
            if not verdicts.empty:
                verdicts["service_date"] = d
                all_verdicts.append(verdicts)
        if all_verdicts:
            verdicts = pd.concat(all_verdicts, ignore_index=True)
            summary = (verdicts.groupby("verdict").size().rename("count")
                       .reset_index())
            sections.append(_md_table(summary))
            hard_missed = verdicts[verdicts["verdict"] == "missed"]
            if not hard_missed.empty:
                sections.append("### Departures that never appeared "
                                "(coverage was verified)\n")
                sections.append(_md_table(
                    hard_missed[["service_date", "scheduled", "headsign", "trip_id"]],
                    max_rows=40))
            if plot_missed(verdicts, os.path.join(out_dir, "missed_departures.png")):
                sections.append("![missed departures](missed_departures.png)\n")
        else:
            sections.append("_No GTFS schedule loaded for these dates — run the "
                            "GTFS loader first._\n")

        # 6: per-physical-vehicle reliability
        sections.append("## 5. Per-vehicle reliability (via imhd výprava)\n")
        vt = vehicles.vehicle_reliability(conn, df)
        if not vt.empty:
            sections.append("Only runs with an unambiguous vehicle↔poradie "
                            "assignment are counted; výprava data is "
                            "daily-granular and marked unverified by imhd.\n")
            sections.append(_md_table(vt))
        else:
            sections.append("_Nothing attributable yet (needs matched runs AND "
                            "výprava rows for the same dates)._\n")

    sections.append(
        "\n---\n"
        "**Known limitations:** whole-minute delay quantisation; ~2-min position "
        "granularity (segment-level, not second-level); the live↔schedule join is "
        "a time/direction/position heuristic, not an id join; výprava is "
        "daily-granular and unverified; stale/parked vehicles are filtered at "
        "analysis time only.\n\n"
        "_Schedule data: GTFS feed © City of Bratislava, CC-BY 4.0. "
        "Vehicle↔poradie assignments: imhd.sk (public výprava page)._\n")

    report_path = os.path.join(out_dir, "report.md")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(sections))
    log.info("report written to %s", report_path)
    return report_path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Build the punctuality report")
    parser.add_argument("--db", default=config.DB_PATH)
    parser.add_argument("--line", default="37")
    parser.add_argument("--since", default=None, help="UTC ISO lower bound, e.g. 2026-07-01")
    parser.add_argument("--until", default=None, help="UTC ISO upper bound")
    parser.add_argument("--out", default=None, help="output directory")
    args = parser.parse_args()

    out_dir = args.out or os.path.join("reports", f"line{args.line}")
    build_report(args.db, args.line, args.since, args.until, out_dir)


if __name__ == "__main__":
    main()
