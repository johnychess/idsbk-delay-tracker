"""Always-on collector loop.

    python -m collector.main

Every SWEEP_INTERVAL_S (default 120 s): sweep all tiling points, dedupe,
append observation rows. Once per day (after VYPRAVA_FETCH_HOUR local):
fetch the imhd.sk výprava table. Once per night (after MATCH_RUN_HOUR):
match recent days against the GTFS schedule. Weekly: refresh the GTFS feed.

Resilience: every unit of work is wrapped in try/except and the DB is
append-only, so a crash or redeploy loses at most one sweep and the loop
resumes cleanly on restart.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import date, datetime, timedelta

import config
import storage
from collector import sweep as sweep_mod
from collector import vyprava as vyprava_mod
from gtfs import loader as gtfs_loader
from match import matcher

log = logging.getLogger("collector")

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    log.info("received signal %s, finishing current cycle", signum)


def _now_local() -> datetime:
    return datetime.now(config.LOCAL_TZ)


def _in_pause_window(now: datetime) -> bool:
    if not config.PAUSE_FROM or not config.PAUSE_TO:
        return False
    hhmm = now.strftime("%H:%M")
    start, end = config.PAUSE_FROM, config.PAUSE_TO
    if start <= end:
        return start <= hhmm < end
    return hhmm >= start or hhmm < end  # window crosses midnight


def _maybe_fetch_vyprava(conn, session) -> None:
    """Once a day (after the configured evening hour), re-fetch every recent
    day that isn't confirmed yet.

    imhd marks a fresh day "recorded automatically, not yet verified"; that
    note disappears once the day is verified (~2 days later). We keep
    re-fetching each unconfirmed day and overwrite it with the verified
    roster the moment the note is gone, then lock it. Provisional data is
    still stored meanwhile so nothing is lost — the confirmed version just
    wins when it arrives."""
    now = _now_local()
    if now.hour < config.VYPRAVA_FETCH_HOUR:
        return
    checked_marker = f"vyprava_checked_{now.date().isoformat()}"
    if storage.get_meta(conn, checked_marker):
        return  # already did today's pass

    fetched_any = False
    for delta in range(config.VYPRAVA_LOOKBACK_DAYS + 1):
        day = now.date() - timedelta(days=delta)
        status_key = f"vyprava_status_{day.isoformat()}"
        if storage.get_meta(conn, status_key) == "confirmed":
            continue  # locked — verified roster already captured
        if fetched_any:
            time.sleep(config.INTER_POINT_DELAY_S)  # be a polite client
        fetched_any = True
        try:
            confirmed = vyprava_mod.collect_vyprava(conn, day, session=session)
            storage.set_meta(conn, status_key,
                             "confirmed" if confirmed else "provisional")
        except Exception:
            log.exception("výprava fetch for %s failed; will retry", day)
    storage.set_meta(conn, checked_marker, "1")


def _maybe_match_recent(conn) -> None:
    """Once a day, join recent observations to the GTFS schedule.

    Matching was a manual step until now, which is exactly why it fell 40 days
    behind without anyone noticing: the analyses that depend on it degrade
    quietly rather than failing, so a stale matched_runs looks like healthy
    collection right up until the missed-departure and per-vehicle sections
    turn out to be hollow.

    Starts at yesterday, never today — a day still in progress would be
    matched against a partial set of runs. match_date upserts, so re-covering
    a day already done is cheap and safe; that is what lets the lookback pick
    up days whose feed arrived late or that were missed during downtime."""
    if not config.MATCH_ENABLED:
        return
    now = _now_local()
    if now.hour < config.MATCH_RUN_HOUR:
        return
    checked_marker = f"match_checked_{now.date().isoformat()}"
    if storage.get_meta(conn, checked_marker):
        return  # already did tonight's pass

    for delta in range(1, config.MATCH_LOOKBACK_DAYS + 1):
        day = now.date() - timedelta(days=delta)
        for line in (config.MATCH_LINES or [None]):
            try:
                matcher.match_date(conn, day, line=line)
            except Exception:
                log.exception("matching %s line=%s failed; will retry tomorrow",
                              day, line or "all")
    storage.set_meta(conn, checked_marker, "1")


def _maybe_refresh_gtfs(conn) -> None:
    last = storage.get_meta(conn, "gtfs_downloaded_at")
    if last:
        age = datetime.now(config.LOCAL_TZ) - datetime.fromisoformat(last)
        if age < timedelta(days=config.GTFS_REFRESH_DAYS):
            return
    try:
        gtfs_loader.refresh(conn)
    except Exception:
        log.exception("GTFS refresh failed; will retry next cycle")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    conn = storage.connect(config.DB_PATH)
    session = sweep_mod.make_session()
    log.info(
        "collector starting: db=%s points=%d interval=%ss pause=%s-%s",
        config.DB_PATH, len(config.SWEEP_POINTS), config.SWEEP_INTERVAL_S,
        config.PAUSE_FROM or "-", config.PAUSE_TO or "-",
    )

    while not _shutdown:
        cycle_started = time.monotonic()

        if _in_pause_window(_now_local()):
            log.debug("in overnight pause window")
        else:
            try:
                rows, stats = sweep_mod.run_sweep(session, config.SWEEP_POINTS)
                storage.insert_observations(conn, rows)
                storage.record_sweep(conn, **stats)
                log.info(
                    "sweep: %d vehicles, %d/%d points ok, %.1fs "
                    "(coverage: %d undercovering, worst margin %s km)",
                    stats["vehicles_seen"],
                    stats["points_queried"] - stats["points_failed"],
                    stats["points_queried"],
                    stats["duration_s"],
                    stats["points_undercovering"],
                    stats["min_reach_margin_km"],
                )
            except Exception:
                log.exception("sweep failed; continuing")

        _maybe_fetch_vyprava(conn, session)
        _maybe_match_recent(conn)
        _maybe_refresh_gtfs(conn)

        elapsed = time.monotonic() - cycle_started
        remaining = max(0.0, config.SWEEP_INTERVAL_S - elapsed)
        deadline = time.monotonic() + remaining
        while not _shutdown and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))

    log.info("collector stopped")


if __name__ == "__main__":
    main()
