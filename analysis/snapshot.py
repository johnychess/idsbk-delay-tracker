"""Freeze a report's numbers as JSON, and diff two of them.

The markdown report is for reading; it is not something you can compare
against. The whole point of collecting a school-holiday baseline is to hold it
up against term-time operation in September, and that comparison has to be
mechanical — eyeballing two 40-row tables invites exactly the kind of
overstatement this project has already had to correct once.

So every report run also writes `snapshot.json`: the same figures, keyed, with
enough provenance to know what they describe. `--compare` then aligns two
snapshots on those keys and prints the deltas.

    # on Railway, at the end of the holiday window
    python -m analysis.snapshot --until 2026-08-31 \
        --label "school holidays 2026" --out baselines/line37-holiday.json

    # once term is underway
    python -m analysis.snapshot --since 2026-09-08 \
        --label "term 2026" --out baselines/line37-term.json
    python -m analysis.snapshot --compare baselines/line37-holiday.json \
                                          baselines/line37-term.json

Comparing snapshots built by different code versions is refused unless they
share a schema_version — a delta between differently-computed numbers is
worse than no delta at all.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from datetime import date, datetime, timezone

import pandas as pd

import config
import storage
from analysis import filters, inherited, missed, punctuality, route_profile

log = logging.getLogger(__name__)

# Bump whenever a recorded figure changes meaning. Snapshots with different
# schema versions are not comparable.
SCHEMA_VERSION = 1

# How many bottleneck segments to keep. The tail is a long flat list of
# near-identical values; the head is the finding.
TOP_BOTTLENECKS = 25


def _num(value):
    """JSON-safe scalar: numpy types out, NaN/Inf to None."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(out) or math.isinf(out):
        return None
    return round(out, 4)


def _records(df: pd.DataFrame, columns: list[str]) -> list[dict]:
    if df is None or df.empty:
        return []
    present = [c for c in columns if c in df.columns]
    out = []
    for row in df[present].to_dict("records"):
        out.append({k: (v if isinstance(v, str) else _num(v))
                    for k, v in row.items()})
    return out


def build_snapshot(conn, line: str, since: str | None, until: str | None,
                   label: str | None = None, df: pd.DataFrame | None = None,
                   filter_report: dict | None = None) -> dict:
    """Compute every headline figure for `line` in the window, as plain data.

    `df` lets a caller that has already loaded, cleaned and match-attached the
    observations hand them straight over — that is how report.py uses this, so
    the snapshot is guaranteed to describe the same rows as the report beside
    it rather than a second, independently recomputed load. Pass
    `filter_report` with it, since attach_matches does not preserve attrs."""
    if df is None:
        raw = filters.load_observations(conn, since=since, until=until, line=line)
        df = filters.clean(raw)
        filter_report = dict(df.attrs.get("filter_report", {}))
        if not df.empty:
            df = punctuality.attach_matches(df, conn)
    freport = dict(filter_report or df.attrs.get("filter_report", {}))

    snap: dict = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "label": label,
        "line": line,
        "on_time_threshold_min": config.ON_TIME_THRESHOLD_MIN,
        "window": {"since": since, "until": until},
        "data_quality": {k: _num(v) for k, v in freport.items()},
        "punctuality": [],
        "headline": {},
        "inherited": {},
        "bottlenecks": [],
        "missed": {},
    }
    if df.empty:
        return snap

    dates = sorted(df["service_date"].unique())
    snap["window"].update({
        "first_service_date": str(dates[0]),
        "last_service_date": str(dates[-1]),
        "service_days": len(dates),
    })

    # Delay LEVELS only from unbiased rows — same rule the report follows.
    levels = df[df["absolute_delay_ok"]] if "absolute_delay_ok" in df.columns else df
    levels = levels.dropna(subset=["delay_minutes"])
    if not levels.empty:
        snap["headline"] = {
            "n": int(len(levels)),
            "median_delay_min": _num(levels["delay_minutes"].median()),
            "mean_delay_min": _num(levels["delay_minutes"].mean()),
            "p90_delay_min": _num(levels["delay_minutes"].quantile(0.90)),
            "pct_on_time": _num(
                100.0 * (levels["delay_minutes"]
                         <= config.ON_TIME_THRESHOLD_MIN).mean()),
        }

    snap["punctuality"] = _records(
        punctuality.punctuality_table(df, by_weekday=False),
        ["line", "direction", "hour", "n", "median", "mean", "p10", "p90",
         "max", "pct_on_time"],
    )

    increments = route_profile.segment_increments(df)
    bottlenecks = route_profile.bottleneck_table(increments)
    snap["bottlenecks"] = _records(
        bottlenecks.head(TOP_BOTTLENECKS),
        ["line", "direction", "from_order", "to_order", "n", "mean_increment",
         "total_mean", "km_per_stop", "increment_per_km", "n_km",
         "mid_lat", "mid_lng"],
    )

    splits = inherited.run_split(df)
    if not splits.empty:
        chains = inherited.chain_circuits(splits)
        snap["inherited"] = {
            "runs": int(len(splits)),
            "median_inherited_min": _num(splits["inherited"].median()),
            "median_gained_min": _num(splits["gained"].median()),
            "circuit_pairs": int(len(chains)) if not chains.empty else 0,
            "median_layover_absorbed_min": (
                _num(chains["layover_absorbed"].median())
                if not chains.empty else None),
        }

    verdicts = []
    for d in dates:
        v = missed.missed_departures(conn, date.fromisoformat(str(d)), line)
        if not v.empty:
            v["service_date"] = d
            verdicts.append(v)
    if verdicts:
        allv = pd.concat(verdicts, ignore_index=True)
        snap["missed"] = {k: int(n) for k, n
                          in allv["verdict"].value_counts().items()}
        snap["missed"]["scheduled_total"] = int(len(allv))
    return snap


def save(snap: dict, path: str) -> str:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, indent=2, ensure_ascii=False, sort_keys=False)
    log.info("snapshot written to %s", path)
    return path


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _delta(before, after):
    if before is None or after is None:
        return None
    return round(after - before, 3)


def _diff_scalars(before: dict, after: dict, keys: list[str]) -> pd.DataFrame:
    rows = []
    for key in keys:
        b, a = before.get(key), after.get(key)
        if b is None and a is None:
            continue
        rows.append({"metric": key, "baseline": b, "current": a,
                     "delta": _delta(b, a)})
    return pd.DataFrame(rows)


def _diff_keyed(before: list[dict], after: list[dict], key_cols: list[str],
                value_cols: list[str]) -> pd.DataFrame:
    """Align two record lists on key_cols and delta each value column.

    Outer join on purpose: a cell present in one snapshot and absent from the
    other is itself a finding (a service that stopped running, an hour that
    started being served), and an inner join would hide it."""
    if not before and not after:
        return pd.DataFrame()
    b = pd.DataFrame(before)
    a = pd.DataFrame(after)
    for frame in (b, a):
        for col in key_cols:
            if col not in frame.columns:
                frame[col] = None
    merged = b.merge(a, on=key_cols, how="outer",
                     suffixes=("_baseline", "_current"))
    for col in value_cols:
        bc, ac = f"{col}_baseline", f"{col}_current"
        if bc in merged.columns and ac in merged.columns:
            merged[f"{col}_delta"] = (merged[ac] - merged[bc]).round(3)
    keep = list(key_cols)
    for col in value_cols:
        keep += [c for c in (f"{col}_baseline", f"{col}_current",
                             f"{col}_delta") if c in merged.columns]
    return merged[keep]


def compare(before: dict, after: dict) -> dict[str, pd.DataFrame]:
    """Baseline vs current, section by section. Raises on schema mismatch."""
    if before.get("schema_version") != after.get("schema_version"):
        raise ValueError(
            f"snapshot schema mismatch: baseline v{before.get('schema_version')} "
            f"vs current v{after.get('schema_version')} — these were computed by "
            "different code and must not be differenced")
    if before.get("line") != after.get("line"):
        raise ValueError(
            f"different lines: {before.get('line')} vs {after.get('line')}")

    punct = _diff_keyed(
        before.get("punctuality", []), after.get("punctuality", []),
        ["line", "direction", "hour"], ["n", "median", "p90", "pct_on_time"])
    if not punct.empty:
        punct = punct.sort_values(["direction", "hour"])

    neck = _diff_keyed(
        before.get("bottlenecks", []), after.get("bottlenecks", []),
        ["line", "direction", "from_order", "to_order"],
        ["n", "mean_increment", "increment_per_km"])
    if not neck.empty and "mean_increment_delta" in neck.columns:
        neck = neck.reindex(
            neck["mean_increment_delta"].abs().sort_values(
                ascending=False).index)

    return {
        "headline": _diff_scalars(
            before.get("headline", {}), after.get("headline", {}),
            ["n", "median_delay_min", "mean_delay_min", "p90_delay_min",
             "pct_on_time"]),
        "inherited": _diff_scalars(
            before.get("inherited", {}), after.get("inherited", {}),
            ["runs", "median_inherited_min", "median_gained_min",
             "circuit_pairs", "median_layover_absorbed_min"]),
        "data_quality": _diff_scalars(
            before.get("data_quality", {}), after.get("data_quality", {}),
            ["raw", "kept", "dropped_stale_parked", "dropped_dead_trip",
             "dropped_implausible_delay", "trips", "runs"]),
        "punctuality": punct,
        "bottlenecks": neck,
    }


def _table(df: pd.DataFrame, max_rows: int = 40) -> str:
    if df is None or df.empty:
        return "_no comparable rows_\n"
    return df.head(max_rows).to_markdown(index=False) + "\n"


def compare_markdown(before: dict, after: dict) -> str:
    diffs = compare(before, after)
    def _name(s):
        return (s.get("label") or s.get("window", {}).get("first_service_date")
                or s.get("generated_at", "?"))

    out = [
        f"# Line {after.get('line')}: {_name(before)} → {_name(after)}",
        f"_Baseline: {before.get('window', {}).get('first_service_date')} … "
        f"{before.get('window', {}).get('last_service_date')} "
        f"({before.get('window', {}).get('service_days')} service days). "
        f"Current: {after.get('window', {}).get('first_service_date')} … "
        f"{after.get('window', {}).get('last_service_date')} "
        f"({after.get('window', {}).get('service_days')} service days)._\n",
        "_`delta` is current minus baseline: NEGATIVE is better for delay "
        "figures, POSITIVE is better for `pct_on_time`._\n",
        "## Headline\n", _table(diffs["headline"]),
        "## Inherited vs gained\n", _table(diffs["inherited"]),
        "## Punctuality by direction × hour\n", _table(diffs["punctuality"], 60),
        "## Bottleneck segments (largest change first)\n",
        _table(diffs["bottlenecks"], 25),
        "## Data quality (sanity — a big swing here means a collection "
        "problem, not a traffic finding)\n",
        _table(diffs["data_quality"]),
        "\n---\n_Both windows must be comparable in kind: same line, same "
        "schema version, and enough service days on each side. A holiday "
        "window has ~20% fewer vehicles in service network-wide, which is the "
        "effect being measured, not a defect._\n",
    ]
    return "\n".join(out)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(
        description="Freeze report figures as JSON, or diff two snapshots")
    parser.add_argument("--db", default=config.DB_PATH)
    parser.add_argument("--line", default="37")
    parser.add_argument("--since", default=None, help="UTC ISO lower bound")
    parser.add_argument("--until", default=None, help="UTC ISO upper bound")
    parser.add_argument("--label", default=None,
                        help="human name for this window, e.g. 'term 2026'")
    parser.add_argument("--out", default=None, help="output path")
    parser.add_argument("--compare", nargs=2, metavar=("BASELINE", "CURRENT"),
                        help="diff two snapshot files instead of building one")
    args = parser.parse_args()

    if args.compare:
        text = compare_markdown(load(args.compare[0]), load(args.compare[1]))
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
            log.info("comparison written to %s", args.out)
        else:
            print(text)
        return

    conn = storage.connect(args.db)
    snap = build_snapshot(conn, args.line, args.since, args.until, args.label)
    out = args.out or os.path.join("reports", f"line{args.line}",
                                   "snapshot.json")
    save(snap, out)
    print(json.dumps(snap.get("headline", {}), indent=2))


if __name__ == "__main__":
    main()
