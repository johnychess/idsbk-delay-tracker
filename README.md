# idsbk-delay-tracker

An always-on **collector** that logs live vehicle positions/delays for
Bratislava public transport (IDS BK / DPB), plus an **analyser** that turns
the accumulated log into answers about reliability. Initial focus: **line 37**
(Záhorská Bystrica ↔ Most SNP); the collector covers **all lines** by default
(it costs the same to run — only more storage).

## Questions it answers

1. Is a line/direction on time or chronically late? (full distribution, not an average)
2. How does delay vary by hour of day and weekday?
3. **Where** on the route do delays appear — which segments are bottlenecks?
4. Is a delay **inherited** ("prenesené") from the previous circuit, or
   **gained on route** ("získané") in traffic?
5. Are scheduled departures **missed entirely** ("vynechané spoje")?
6. Does a specific **physical vehicle** chronically run late?

## Data sources (all public, no authentication)

| Source | What it gives | Cadence |
|---|---|---|
| `mapa.idsbk.sk/navigation/vehicles/nearby` | live positions + `delayMinutes` | sweep every 120 s |
| Bratislava GTFS (CC-BY 4.0, © City of Bratislava) | the schedule, incl. run numbers | weekly refresh |
| `imhd.sk/ba/vyprava` (public page) | actual daily physical-vehicle ↔ poradie assignment | once per day |

### The quirks the whole design is built around

- **`radius` is ignored** by the live endpoint: every call returns the 100
  vehicles nearest the query point. The collector therefore *tiles* the city
  with ~16 points and dedupes by `vehicleID`.
- **`vehicleID == tripID`** — it identifies a scheduled *run instance*, not a
  physical bus; `licenseNumber` is almost always null. Physical identity comes
  from the výprava page instead.
- Vehicles report **~every 2 minutes** → the sweep interval is 120 s; faster
  polling only yields duplicates.
- The live feed's `spoj` number does **not** id-join to GTFS
  (`trip_short_name` is empty). The join is a **heuristic**: line + direction +
  nearest scheduled time + position consistency (`match/matcher.py`).
- The run number ("poradie") is **encoded in the GTFS `trip_id`**
  (`37012_03_5_18181` → poradie `03`); `block_id` is empty (`gtfs/poradie.py`).
- **Dirty data exists** (parked vehicles with stale trips showing 500+ min).
  The raw log is kept intact; filtering happens at analysis time
  (`analysis/filters.py`).
- imhd's realtime `/rt/` endpoints are **login-gated — never used or bypassed**;
  only the public výprava page is read, once per day.

## Repo layout

```
collector/   sweep loop (tiling + dedupe), výprava fetch/parse, main daemon
gtfs/        feed download/load, service-calendar resolver, trip_id→poradie
match/       heuristic live↔schedule join → matched_runs table
analysis/    filters + the six analyses + report/plots
config.py    all tunables (env-overridable, see .env.example)
storage.py   SQLite schema (append-only observations, WAL)
data/        the SQLite DB (on the Railway Volume in production)
```

## Quickstart (local)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python -m gtfs.loader              # download & load the GTFS feed
python -m collector.main           # start collecting (Ctrl-C to stop)
```

After at least a day of data:

```bash
python -m match.matcher --date 2026-07-01 --line 37   # live↔GTFS join
python -m analysis.report --line 37                   # report + plots
# → reports/line37/report.md, *.png, bottleneck_map.html
```

Run the matcher for each collected date (a daily cron/loop is fine — it is
idempotent). The report needs matched runs for the poradie/vehicle analyses.

## Deployment (Railway)

Deploy as a **background worker** — no web port.

1. Create a service from this repo (the `Dockerfile` is picked up via
   `railway.json`).
2. **Attach a Volume and mount it at `/data`** — mandatory. The container
   filesystem is wiped on every redeploy; without the Volume you lose the DB.
   `DB_PATH` defaults to `/data/tracker.sqlite` in the image.
3. Optionally set env vars from `.env.example` (coverage, pause window, …).

Footprint is tiny (~0.05 vCPU, ~150 MB RAM; whole-network log < ~200 MB/week).
Railway bills egress only; the traffic here is almost all ingress, so a week
of running costs on the order of **$0.5–1** (the Trial credit covers it).

## Being a polite client

120 s cadence, ~1 s between point queries, honest User-Agent, one výprava
page per day, one GTFS download per week. Don't crank the knobs down.

## Known limitations (also stated in reports)

- `delayMinutes` is whole-minute quantised → good aggregates, noisy single trips.
- ~2-min position granularity → segment-level, not second-level, localisation.
- The live↔schedule join is heuristic (time/direction/position), not an id join.
- The výprava table is daily-granular and marked "not yet verified" by imhd.
- Stale/parked vehicles produce outliers → filtered at analysis time only.

## Attribution

Schedule data: GTFS feed © City of Bratislava, licensed CC-BY 4.0.
Daily vehicle↔run assignments: imhd.sk public výprava page.
