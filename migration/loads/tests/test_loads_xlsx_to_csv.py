"""xlsx_to_csv.py: the tFileInputExcel replacement produces a CSV that OPS.FF_CSV_EXTRACT / SP_COPY_RAW load."""

from __future__ import annotations

import datetime as dt

import openpyxl
import pytest

import loads_duckdb_mirror as mirror
import xlsx_to_csv

EXPECTED = """\
transaction_id,account_id,transaction_date,amount,transaction_type,branch_id
6,6,2024-01-18 13:10:00,50000,Withdrawal,1
7,6,2024-01-19 14:00:00,100000,Payment,1
11,10,2024-01-20 15:00:00,1000000,Transfer,1
12,11,2024-01-20 10:00:00,500000,Deposit,1
13,12,2024-01-20 12:10:00,500000,Withdrawal,5
14,13,2024-01-21 14:00:00,1500000,Deposit,4
15,14,2024-01-21 08:00:00,500000,Transfer,3
"""


def test_transaction_excel_converts_to_expected_csv(excel_csv):
    assert excel_csv.read_bytes() == EXPECTED.encode("utf-8")


def test_cli(tmp_path, capsys):
    out = tmp_path / "nested" / "out.csv"
    assert xlsx_to_csv.main([str(mirror.DATA / "transaction_excel.xlsx"), str(out), "--sheet", "Sheet1"]) == 0
    assert out.read_text(encoding="utf-8") == EXPECTED
    assert "wrote 7 rows" in capsys.readouterr().out


def test_cli_reports_missing_sheet(tmp_path, capsys):
    rc = xlsx_to_csv.main([str(mirror.DATA / "transaction_excel.xlsx"), str(tmp_path / "o.csv"), "--sheet", "Nope"])
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def _workbook(tmp_path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    path = tmp_path / "book.xlsx"
    wb.save(path)
    return path


def test_cell_formatting_and_blank_rows(tmp_path):
    header = list(xlsx_to_csv.TRANSACTION_COLUMNS)
    path = _workbook(
        tmp_path,
        [
            header,
            [1, 2.0, dt.datetime(2024, 1, 18, 13, 9, 59, 999_600), 10.5, "Pay, \"quoted\"", None],
            [None, None, None, None, None, None],
            [2, 3, dt.datetime(2024, 2, 1, 8, 0), 7, "Deposit", 4],
        ],
    )
    out = tmp_path / "out.csv"
    assert xlsx_to_csv.convert(path, out) == 2
    assert out.read_text(encoding="utf-8").splitlines() == [
        ",".join(header),
        '1,2,2024-01-18 13:10:00,10.5,"Pay, ""quoted""",',
        "2,3,2024-02-01 08:00:00,7,Deposit,4",
    ]


def test_header_mismatch_is_an_error(tmp_path):
    path = _workbook(tmp_path, [["id", "amount"], [1, 2]])
    with pytest.raises(xlsx_to_csv.ConversionError, match="expected"):
        xlsx_to_csv.convert(path, tmp_path / "out.csv")
    assert xlsx_to_csv.convert(path, tmp_path / "out.csv", expected_columns=None) == 1


def test_values_beyond_header_are_an_error(tmp_path):
    path = _workbook(tmp_path, [["a", "b"], [1, 2, 3]])
    with pytest.raises(xlsx_to_csv.ConversionError, match="beyond"):
        xlsx_to_csv.convert(path, tmp_path / "out.csv", expected_columns=None)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, ""),
        (True, "TRUE"),
        (dt.date(2024, 1, 2), "2024-01-02"),
        (dt.time(9, 5), "09:05:00"),
        (3.0, "3"),
        (0.25, "0.25"),
        ("x", "x"),
    ],
)
def test_format_cell(value, text):
    assert xlsx_to_csv.format_cell(value) == text
