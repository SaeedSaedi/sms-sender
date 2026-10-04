from pathlib import Path

import pytest

from sms_sender.input_loader import InputError, TokenColumns, load


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
    # The header row is skipped, not recorded as an invalid phone.
    assert [x.phone for x in r.valid] == ["09123456789", "09120000000"]
    assert r.invalid == []
    assert r.header == "phone"


def test_token_columns_apply_kavenegar_token_rules(tmp_path):
    p = write(
        tmp_path, "in.csv",
        "phone,coin\n"
        "09120000001,tether_usdt\n"     # underscore → Kavenegar error 431
        f"09120000002,{'x' * 101}\n"     # over 100 characters
        "09120000003,tether-usdt\n",
    )
    r = load(p, TokenColumns(columns={"token20": "coin"}))
    assert [x.phone for x in r.valid] == ["09120000003"]
    reasons = {x.raw: x.reason for x in r.invalid}
    assert "'_'" in reasons["09120000001"] and "coin" in reasons["09120000001"]
    assert "at most 100" in reasons["09120000002"]


def test_excel_style_header_is_skipped_not_invalid(tmp_path):
    # Real segment exports: BOM + "Phone Number" + one number per row.
    p = tmp_path / "seg.csv"
    p.write_bytes("﻿Phone Number\n09123456789\n09120000000\n".encode("utf-8"))
    r = load(p)
    assert [x.phone for x in r.valid] == ["09123456789", "09120000000"]
    assert r.invalid == []
    assert r.header == "Phone Number"


def test_only_the_first_row_can_be_a_header(tmp_path):
    p = write(tmp_path, "in.txt", "Phone Number\nnope\n09123456789\n")
    r = load(p)
    assert r.header == "Phone Number"
    assert [x.raw for x in r.invalid] == ["nope"]


def test_first_row_with_digits_is_data(tmp_path):
    p = write(tmp_path, "in.txt", "0812345678\n09123456789\n")
    r = load(p)
    assert r.header is None
    assert [x.raw for x in r.invalid] == ["0812345678"]


def test_persian_digit_first_row_is_data(tmp_path):
    p = write(tmp_path, "in.txt", "۰۹۱۲۳۴۵۶۷۸۹\n")
    r = load(p)
    assert r.header is None
    assert [x.phone for x in r.valid] == ["09123456789"]


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


# ---------- per-recipient token columns ----------

TRADE_SPEC = TokenColumns(
    columns={"token": "trade_side", "token10": "first_name", "token20": "last_token"},
    value_maps={"trade_side": {"Buy": "خرید", "Sell": "فروش"}},
)
TRADE_HEADER = "phone_number,first_name,last_token,trade_side\n"


def test_token_columns_read_per_row_values(tmp_path):
    p = write(tmp_path, "in.csv", TRADE_HEADER
              + "09120000001,علی,ترون,Buy\n"
              + "+989120000002,محمد رضا, پالیگان اکوسیستم ,Sell\n")
    r = load(p, TRADE_SPEC)
    assert [x.phone for x in r.valid] == ["09120000001", "09120000002"]
    assert r.valid[0].tokens == {"token": "خرید", "token10": "علی", "token20": "ترون"}
    # Cells are trimmed; trade_side goes through the value map.
    assert r.valid[1].tokens == {
        "token": "فروش", "token10": "محمد رضا", "token20": "پالیگان اکوسیستم",
    }
    assert r.invalid == []  # the header row is not mistaken for a phone


def test_token_columns_reject_unsendable_rows(tmp_path):
    p = write(tmp_path, "in.csv", TRADE_HEADER
              + "09120000001,علی,ترون,Hold\n"           # not in the value map
              + "09120000002,,ترون,Buy\n"               # empty name
              + "09120000003,a b c d e f g,ترون,Buy\n"  # 6 spaces > token10's 5
              + "09120000004,رضا,تتر,Sell\n")
    r = load(p, TRADE_SPEC)
    assert [x.phone for x in r.valid] == ["09120000004"]
    reasons = {x.raw: x.reason for x in r.invalid}
    assert "value map" in reasons["09120000001"]
    assert "empty" in reasons["09120000002"]
    assert "token10" in reasons["09120000003"]
    assert [x.line_no for x in r.invalid] == [2, 3, 4]


def test_token_columns_dedup_keeps_first_row(tmp_path):
    p = write(tmp_path, "in.csv", TRADE_HEADER
              + "09120000001,علی,ترون,Buy\n"
              + "+989120000001,سارا,تتر,Sell\n")
    r = load(p, TRADE_SPEC)
    assert [x.tokens["token10"] for x in r.valid] == ["علی"]
    assert r.duplicates_collapsed == 1


def test_token_columns_missing_column_is_an_input_error(tmp_path):
    p = write(tmp_path, "in.csv", "phone,name\n09120000001,Ali\n")
    with pytest.raises(InputError, match="first_name"):
        load(p, TokenColumns(columns={"token10": "first_name"}))


def test_invalid_rows_carry_a_key_for_the_dashboard(tmp_path):
    p = tmp_path / "in.csv"
    p.write_text(
        "phone,user_id,side\n"
        "09120000001,u1,Buy\n"
        "nope,u2,Buy\n"
        "09120000003,u3,\n"
        "09120000004,u4,Hold\n"
        "09120000005,u5,Buy\n"
        "09120000005,u6,Buy\n",
        encoding="utf-8",
    )
    spec = TokenColumns(columns={"token": "side"}, value_maps={"side": {"Buy": "خرید"}})
    result = load(p, spec, "user_id")
    assert [(r.raw, r.key) for r in result.invalid] == [
        ("nope", "invalid_phone"),
        ("09120000003", "empty_value"),
        ("09120000004", "unmapped_value"),
        ("09120000005", "conflicting_user_ids"),
        ("09120000005", "conflicting_user_ids"),
    ]
