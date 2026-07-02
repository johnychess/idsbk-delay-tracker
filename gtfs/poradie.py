"""Decode the run number ("poradie" / "kurz" / výprava) from a GTFS trip_id.

DPB leaves both `trip_short_name` and `block_id` empty, but encodes the run
number as the 2nd underscore-separated segment of `trip_id`:

    37012_03_5_18181  ->  poradie "03"  (route 37012 = line 37)

Line 37 uses poradia 01-06 (all-day) plus 07-11 and 51 (peak/special).

`normalize_poradie` strips leading zeros so GTFS "03" joins with imhd
výprava "3" (výprava sub-runs like "2a" keep their letter).
"""

from __future__ import annotations


def poradie_from_trip_id(trip_id: str) -> str | None:
    parts = trip_id.split("_")
    if len(parts) < 2 or not parts[1]:
        return None
    return parts[1]


def normalize_poradie(poradie: str | None) -> str | None:
    if not poradie:
        return None
    p = poradie.strip().lower()
    base = p.rstrip("abcdefgh")
    suffix = p[len(base):]
    base = base.lstrip("0") or "0"
    return base + suffix
