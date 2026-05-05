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
