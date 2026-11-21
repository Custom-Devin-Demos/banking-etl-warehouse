"""Convert an Excel worksheet to a CSV that Snowflake COPY INTO can load (replaces Talend tFileInputExcel).

COPY INTO cannot parse .xlsx, so the workbook is converted before it is staged:

    python migration/loads/xlsx_to_csv.py data_sources/transaction_excel.xlsx build/transaction_excel.csv
    PUT file://build/transaction_excel.csv @OPS.LANDING_STAGE/files/transaction_excel/ AUTO_COMPRESS=TRUE;

Output matches OPS.FF_CSV_EXTRACT (migration/snowflake/loads/00_file_formats.sql): UTF-8, comma, header row,
"\\n" line endings, RFC 4180 quoting, empty field for NULL, datetimes as YYYY-MM-DD HH24:MI:SS (rounded to the
second, so Excel float noise such as 13:09:59.9999 becomes 13:10:00), dates as YYYY-MM-DD, whole-number floats
as integers. Cell values are converted from their stored type, not their display format (the m/d/yy h:mm format
in the workbook is ignored), as Talend did.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

import openpyxl

TRANSACTION_COLUMNS = (
    "transaction_id",
    "account_id",
    "transaction_date",
    "amount",
    "transaction_type",
    "branch_id",
)


class ConversionError(ValueError):
    pass


def format_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, dt.datetime):
        rounded = (value + dt.timedelta(microseconds=500_000)).replace(microsecond=0)
        return rounded.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, dt.time):
        return value.strftime("%H:%M:%S")
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value)


def read_rows(xlsx_path: Path, sheet: str | None = None, header_rows: int = 1) -> tuple[list[str], list[list[str]]]:
    workbook = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    try:
        if sheet is None:
            worksheet = workbook.worksheets[0]
        elif sheet in workbook.sheetnames:
            worksheet = workbook[sheet]
        else:
            raise ConversionError(f"sheet {sheet!r} not found in {xlsx_path} (sheets: {workbook.sheetnames})")
        rows = [list(row) for row in worksheet.iter_rows(values_only=True)]
    finally:
        workbook.close()

    if len(rows) < header_rows or header_rows < 1:
        raise ConversionError(f"{xlsx_path}: expected {header_rows} header row(s)")
    header = [format_cell(v).strip() for v in rows[header_rows - 1]]
    while header and header[-1] == "":
        header.pop()
    if not header or "" in header:
        raise ConversionError(f"{xlsx_path}: blank column name in header {header}")

    width = len(header)
    data: list[list[str]] = []
    for number, row in enumerate(rows[header_rows:], start=header_rows + 1):
        cells = [format_cell(v) for v in row]
        if all(c == "" for c in cells):
            continue
        if any(c != "" for c in cells[width:]):
            raise ConversionError(f"{xlsx_path}: row {number} has values beyond the {width} header columns")
        data.append((cells + [""] * width)[:width])
    return header, data


def write_csv(header: Sequence[str], rows: Iterable[Sequence[str]], out) -> int:
    writer = csv.writer(out, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    count = 0
    for row in rows:
        writer.writerow(row)
        count += 1
    return count


def convert(
    xlsx_path: Path,
    csv_path: Path,
    sheet: str | None = None,
    header_rows: int = 1,
    expected_columns: Sequence[str] | None = TRANSACTION_COLUMNS,
) -> int:
    header, rows = read_rows(xlsx_path, sheet=sheet, header_rows=header_rows)
    if expected_columns is not None and [h.lower() for h in header] != list(expected_columns):
        raise ConversionError(f"{xlsx_path}: header {header} != expected {list(expected_columns)}")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as out:
        return write_csv(header, rows, out)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("xlsx", type=Path)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--sheet", help="worksheet name (default: first sheet; Talend job used Sheet1)")
    parser.add_argument("--header-rows", type=int, default=1, help="Talend HEADER (default 1)")
    parser.add_argument(
        "--any-columns",
        action="store_true",
        help=f"do not require the transaction header {','.join(TRANSACTION_COLUMNS)}",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        count = convert(
            args.xlsx,
            args.csv,
            sheet=args.sheet,
            header_rows=args.header_rows,
            expected_columns=None if args.any_columns else TRANSACTION_COLUMNS,
        )
    except ConversionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {count} rows to {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
