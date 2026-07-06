from datetime import datetime, timedelta
from unittest.mock import patch

import config
import storage
from collector import vyprava as vyprava_mod
from collector.vyprava import is_confirmed, note_absent, parse_vyprava

HTML = """
<html><body>
<h2>Výprava vozidiel</h2>
<table>
  <tr><th>Linka</th><th>Vozidlá</th></tr>
  <tr><td>1</td><td>7501/1, 7502/2</td></tr>
  <tr><td>37</td><td>3319/1, 3403/2, 3381/2a, 3320/2b, 3377/3, 3317/4,
      3315/4a, 3322/4b, 3322/5, 3314/6</td></tr>
  <tr><td>N33</td><td>2801/1</td></tr>
  <tr><td>poznámka</td><td>zaznamenané automaticky</td></tr>
</table>
</body></html>
"""


def test_parse_vyprava_lines_and_entries():
    entries = parse_vyprava(HTML)
    line37 = [(p, v) for line, p, v in entries if line == "37"]
    assert ("1", "3319") in line37
    assert ("2a", "3381") in line37
    assert ("2b", "3320") in line37
    # same vehicle on two poradia is legitimate (mid-day swap)
    assert ("4b", "3322") in line37 and ("5", "3322") in line37
    assert len(line37) == 10

    assert ("1", "1", "7501") in entries
    assert ("N33", "1", "2801") in entries
    # the note row has no line token and no entry pattern
    assert all(line != "poznámka" for line, _, _ in entries)


def test_parse_vyprava_empty_page():
    assert parse_vyprava("<html><body>nothing here</body></html>") == []


PROVISIONAL_HTML = ("<html><body><h2>Výprava ku dňu (nedeľa, včera)</h2>"
                    "<p>Tieto údaje boli zaznamenané automatizovane a ešte "
                    "neboli verifikované.</p>"
                    "<table><tr><td>3</td><td>7524/1</td></tr></table></body></html>")
NOTE_GONE_HTML = ("<html><body><h2>Výprava ku dňu (sobota)</h2>"
                  "<table><tr><td>3</td><td>7524/1</td></tr></table></body></html>")


def test_note_absent():
    assert note_absent(PROVISIONAL_HTML) is False
    assert note_absent(NOTE_GONE_HTML) is True
    assert is_confirmed is note_absent  # back-compat alias


def _collect_with(html, day, tmp_path):
    conn = storage.connect(str(tmp_path / "t.sqlite"))
    with patch.object(vyprava_mod, "fetch_vyprava_html", lambda d, session=None: html):
        confirmed = vyprava_mod.collect_vyprava(conn, day)
    stored = conn.execute("SELECT confirmed FROM vyprava WHERE date=?",
                          (day.isoformat(),)).fetchall()
    return confirmed, stored


def test_confirmed_only_when_note_gone_and_old_enough(tmp_path):
    today = datetime.now(config.LOCAL_TZ).date()
    old_day = today - timedelta(days=config.VYPRAVA_MIN_CONFIRM_AGE_DAYS)
    # note gone AND old enough -> confirmed
    confirmed, stored = _collect_with(NOTE_GONE_HTML, old_day, tmp_path)
    assert confirmed is True
    assert stored == [(1,)]


def test_fresh_day_never_confirmed_even_if_note_gone(tmp_path):
    today = datetime.now(config.LOCAL_TZ).date()  # age 0 < MIN_CONFIRM_AGE_DAYS
    # note gone but too fresh -> stays provisional (guards against reworded marker)
    confirmed, stored = _collect_with(NOTE_GONE_HTML, today, tmp_path)
    assert confirmed is False
    assert stored == [(0,)]


def test_note_present_is_provisional_regardless_of_age(tmp_path):
    old_day = datetime.now(config.LOCAL_TZ).date() - timedelta(days=10)
    confirmed, stored = _collect_with(PROVISIONAL_HTML, old_day, tmp_path)
    assert confirmed is False
    assert stored == [(0,)]


def test_replace_vyprava_overwrites_provisional_with_confirmed(tmp_path):
    conn = storage.connect(str(tmp_path / "t.sqlite"))
    # provisional roster: circuit 22 was (wrongly) vehicle 9999
    storage.replace_vyprava(conn, "2026-07-05", [("3", "22", "9999")],
                            "2026-07-05T20:00:00Z", confirmed=False)
    row = conn.execute("SELECT vehicle, confirmed FROM vyprava WHERE date='2026-07-05'"
                       " AND poradie='22'").fetchone()
    assert row == ("9999", 0)

    # confirmed roster corrects it to 7533 — must overwrite, not accumulate
    storage.replace_vyprava(conn, "2026-07-05", [("3", "22", "7533")],
                            "2026-07-07T20:00:00Z", confirmed=True)
    rows = conn.execute("SELECT vehicle, confirmed FROM vyprava WHERE date='2026-07-05'"
                        " AND poradie='22'").fetchall()
    assert rows == [("7533", 1)]  # single corrected, confirmed row
