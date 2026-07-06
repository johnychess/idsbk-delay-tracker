import storage
from collector.vyprava import is_confirmed, parse_vyprava

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


def test_is_confirmed():
    provisional = ("<html><body><h2>Výprava ku dňu 5.7.2026 (nedeľa, včera)</h2>"
                   "<p>Tieto údaje boli zaznamenané automatizovane a ešte "
                   "neboli verifikované.</p></body></html>")
    confirmed = ("<html><body><h2>Výprava ku dňu 4.7.2026 (sobota)</h2>"
                 "<table><tr><td>3</td><td>7524/1</td></tr></table></body></html>")
    assert is_confirmed(provisional) is False
    assert is_confirmed(confirmed) is True


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
