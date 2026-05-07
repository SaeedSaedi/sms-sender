from pathlib import Path

from sms_sender.input_loader import load


def write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_txt_basic(tmp_path):
    p = write(tmp_path, "in.txt", "09123456789\n09120000000\n")
    r = load(p)
    assert [x.phone for x in r.valid] == ["09123456789", "09120000000"]
    assert r.invalid == []
    assert r.duplicates_collapsed == 0


def test_txt_skips_blanks_and_comments(tmp_path):
    p = write(tmp_path, "in.txt", "\n# header\n09123456789\n\n  \n")
    r = load(p)
    assert [x.phone for x in r.valid] == ["09123456789"]


def test_csv_takes_first_cell(tmp_path):
    p = write(tmp_path, "in.csv", "phone,name\n09123456789,Ali\n+989120000000,Sara\n")
    r = load(p)
    # header row is invalid (not a phone), real rows succeed
    assert [x.phone for x in r.valid] == ["09123456789", "09120000000"]
    assert len(r.invalid) == 1
    assert r.invalid[0].raw == "phone"


def test_dedup(tmp_path):
    p = write(tmp_path, "in.txt", "09123456789\n+989123456789\n9123456789\n")
    r = load(p)
    assert [x.phone for x in r.valid] == ["09123456789"]
    assert r.duplicates_collapsed == 2


def test_invalid_collected(tmp_path):
    p = write(tmp_path, "in.txt", "09123456789\nnope\n0812345678\n")
    r = load(p)
    assert [x.phone for x in r.valid] == ["09123456789"]
    assert {x.raw for x in r.invalid} == {"nope", "0812345678"}
    assert all(x.line_no > 0 for x in r.invalid)


def test_bom_handled(tmp_path):
    # utf-8-sig opener strips BOM transparently.
    p = tmp_path / "bom.txt"
    p.write_bytes("﻿09123456789\n".encode("utf-8"))
    r = load(p)
    assert [x.phone for x in r.valid] == ["09123456789"]


# ---------- _extract_first_cell edge cases ----------


def test_csv_quoted_first_cell(tmp_path):
    """A CSV cell wrapped in double quotes (Excel default for cells with
    commas/special chars) should still yield the inner value."""
    p = write(tmp_path, "in.csv", '"09123456789",Ali\n"+989120000000",Sara\n')
    r = load(p)
    assert [x.phone for x in r.valid] == ["09123456789", "09120000000"]


def test_csv_first_cell_blank_falls_through(tmp_path):
    """If the first cell is blank, the next non-blank cell wins."""
    p = write(tmp_path, "in.csv", ",,09123456789\n")
    r = load(p)
    assert [x.phone for x in r.valid] == ["09123456789"]


def test_csv_all_blank_row_skipped(tmp_path):
    p = write(tmp_path, "in.csv", ",,,\n09123456789\n")
    r = load(p)
    assert [x.phone for x in r.valid] == ["09123456789"]


def test_csv_embedded_comma_in_quoted_cell(tmp_path):
    """`"123,456",foo` → first cell is `123,456`, not `123`."""
    p = write(tmp_path, "in.csv", '"123,456",foo\n09123456789,bar\n')
    r = load(p)
    # First row's first cell is invalid as a phone but well-formed as CSV.
    assert {x.raw for x in r.invalid} == {"123,456"}
    assert [x.phone for x in r.valid] == ["09123456789"]


def test_txt_persian_digits_with_separators(tmp_path):
    """Persian digits + dashes + parens — Excel exports often look like this."""
    p = write(tmp_path, "in.txt", "(۰۹۱۲) ۳۴۵-۶۷۸۹\n")
    r = load(p)
    assert [x.phone for x in r.valid] == ["09123456789"]
