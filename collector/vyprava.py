"""Fetch and parse the imhd.sk daily "výprava" page: the actual (not just
planned) assignment of physical vehicles (evidenčné čísla) to runs (poradia),
per line, per day.

    Today:      https://imhd.sk/ba/vyprava
    Historical: https://imhd.sk/ba/vyprava?d=YYYY-MM-DD

The page is server-rendered HTML with a per-line table of
"evidenčnéČíslo/poradie" entries, e.g. for line 37:

    3319/1, 3403/2, 3381/2a, 3320/2b, 3377/3, ...

meaning vehicle 3319 ran poradie 1, vehicle 3403 ran poradie 2, etc.
`a`/`b` suffixes are sub-runs (peak fill-ins / mid-day swaps); the same
vehicle can appear on several poradia and lines during one day.

Caveats (also surface these in analysis outputs):
- the page is marked "recorded automatically, not yet verified",
- it is daily-granular — no exact swap timestamps.

IMPORTANT: do NOT use imhd's live realtime endpoints (/rt/...) — they are
login-gated (401). This public page is the open substitute.

NOTE: the row-structure heuristics below were written against the described
table layout; verify once against the live page and tighten selectors if
imhd's markup differs (the entry regex itself is the load-bearing part and
is layout-independent).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone

import requests
from bs4 import BeautifulSoup

import config
import storage

log = logging.getLogger(__name__)

# "3319/1", "3381/2a", "7501/12" — evidenčné číslo / poradie(+sub-run letter)
ENTRY_RE = re.compile(r"\b(\d{2,5})\s*/\s*(\d{1,2}[a-z]?)\b")

# A line label: "1".."99", "N33", "X6", "212", "S65" ... short alnum token.
LINE_RE = re.compile(r"^[A-ZŠČŽ]?\d{1,3}[A-Z]?$")

# Present while a day is still provisional ("...recorded automatically and not
# yet verified"); it disappears once imhd verifies the day. Absence of it (on a
# page that actually has roster rows) means the day is confirmed/final.
PROVISIONAL_MARKER = "neboli verifikovan"


def note_absent(html: str) -> bool:
    """True when the 'not yet verified' note is gone from the page.

    This is only the raw signal — collect_vyprava also age-gates it (a day
    isn't trusted as confirmed until it's a couple of days old), so that a
    reworded/removed marker can't silently lock fresh provisional data."""
    return PROVISIONAL_MARKER not in html.lower()


# Back-compat alias: the raw note check. Prefer the age-gated decision in
# collect_vyprava for "is this day final".
is_confirmed = note_absent


def fetch_vyprava_html(day: date, session: requests.Session | None = None) -> str:
    sess = session or requests.Session()
    resp = sess.get(
        config.VYPRAVA_URL,
        params={"d": day.isoformat()},
        headers={"User-Agent": config.USER_AGENT},
        timeout=config.REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.text


def parse_vyprava(html: str) -> list[tuple[str, str, str]]:
    """Return (line, poradie, vehicle) tuples.

    Strategy: walk table rows; a row belongs to a line when one cell is a
    short line-like token and the rest of the row contains vehicle/poradie
    entries. This survives cosmetic markup changes because the entry pattern
    "1234/5a" is unambiguous.
    """
    soup = BeautifulSoup(html, "html.parser")
    results: list[tuple[str, str, str]] = []

    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if not cells:
            continue
        line = None
        entries: list[tuple[str, str]] = []
        for text in cells:
            if line is None and LINE_RE.match(text):
                line = text
                continue
            entries.extend((m.group(1), m.group(2)) for m in ENTRY_RE.finditer(text))
        if line and entries:
            results.extend((line, poradie, vehicle) for vehicle, poradie in entries)

    if not results:
        log.warning(
            "výprava parse produced 0 entries (%d bytes of HTML) — "
            "the page layout may have changed; inspect it manually",
            len(html),
        )
    return results


def collect_vyprava(conn, day: date, session: requests.Session | None = None) -> bool:
    """Fetch + parse + store one day, replacing any prior rows for that date so
    a confirmation/correction overwrites an earlier provisional fetch. Returns
    True if the stored day is confirmed (verified), False if still provisional.

    If the page yields no entries (transient error / layout change), prior data
    is left untouched and False is returned so the day stays unlocked."""
    html = fetch_vyprava_html(day, session=session)
    entries = parse_vyprava(html)
    if not entries:
        log.warning("výprava %s: 0 entries parsed; leaving any prior data intact", day)
        return False

    verified_note_gone = note_absent(html)
    age_days = (datetime.now(config.LOCAL_TZ).date() - day).days
    confirmed = verified_note_gone and age_days >= config.VYPRAVA_MIN_CONFIRM_AGE_DAYS
    if verified_note_gone and not confirmed:
        # The note is gone but the day is too fresh to have been verified —
        # imhd may have reworded/removed the marker. Stay provisional and shout.
        log.warning(
            "výprava %s: verification note absent on a %d-day-old page — treating "
            "as provisional; the imhd marker text may have changed (check %r)",
            day, age_days, PROVISIONAL_MARKER,
        )

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    storage.replace_vyprava(conn, day.isoformat(), entries, fetched_at, confirmed)
    log.info("výprava %s: %d entries stored (%s)", day, len(entries),
             "confirmed" if confirmed else "provisional")
    return confirmed
