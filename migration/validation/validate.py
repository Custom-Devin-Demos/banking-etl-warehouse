#!/usr/bin/env python3
"""Synapse/SQL Server -> Snowflake migration validation harness.

Compares, for every RAW / DWH / OPS table and both reporting procedures, the row count, key sums
(SUM of every key and amount column) and checksums (full per-row SHA-256 multiset hash, plus a
deterministic hash-bucket sample for tables flagged as large) between:

* expected values computed in Python from ``data_sources/`` (RAW), ``migration/baseline/`` (DWH,
  procedures) or the documented Talend rules applied to the extracts (OPS rejects), and
* actual values computed *inside Snowflake* by the committed SQL under ``sql/``.

Both sides use the canonicalisation documented in ``README.md`` (see ``canon_*`` below and
``_sql_canon``), so a row produces the same SHA-256 in Python and in Snowflake.

Modes
-----
``--offline``   No Snowflake. Applies the Talend transform rules (PLAN.md 1.5/1.6) to the source
                extracts and checks the result against ``migration/baseline/``; also checks the
                committed SQL is in sync with the generator. This is what CI runs.
(default)       Live. Needs ``SNOWFLAKE_ACCOUNT`` (PLAN.md 3); skips cleanly with exit 0 when unset
                (``--require-live`` turns the skip into exit 2).
``--write-sql`` Regenerate ``sql/`` from the table specs.

Exit codes: 0 = all checks passed (or live skipped), 1 = mismatch, 2 = configuration/connection error.

``snowflake.connector`` is imported lazily, only when a live connection is opened.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from xml.etree import ElementTree

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
SQL_DIR = HERE / "sql"
DEFAULT_DATA_DIR = REPO_ROOT / "data_sources"
DEFAULT_BASELINE_DIR = REPO_ROOT / "migration" / "baseline"

# --------------------------------------------------------------------------------------------
# Canonicalisation (must stay byte-for-byte identical to _sql_canon)
# --------------------------------------------------------------------------------------------
FIELD_SEP = "\x1f"  # CHR(31), ASCII unit separator: joins canonical fields of one row
NULL_MARKER = "\x1e"  # CHR(30), ASCII record separator: stands for SQL NULL
HASH_SEP = ","  # joins sorted row hashes before the table-level hash

KINDS = ("int", "dec4", "date", "ts", "str")
_Q0 = Decimal("1")
_Q4 = Decimal("0.0001")


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value).strip())


def _fmt_decimal(value: Decimal, quantum: Decimal) -> str:
    q = value.quantize(quantum, rounding=ROUND_HALF_UP)  # Snowflake CAST rounds half away from 0
    if q == 0:
        q = abs(q)  # no negative zero
    return format(q, "f")


def canon_int(value: Any) -> str | None:
    """CAST(x AS NUMBER(38,0)) -> TO_VARCHAR: plain integer, no sign for zero, no separators."""
    return None if value is None else _fmt_decimal(_to_decimal(value), _Q0)


def canon_dec4(value: Any) -> str | None:
    """CAST(x AS NUMBER(38,4)) -> TO_VARCHAR: fixed 4 decimals (SQL Server MONEY scale)."""
    return None if value is None else _fmt_decimal(_to_decimal(value), _Q4)


def _to_datetime(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognised timestamp {value!r}")


def canon_ts(value: Any) -> str | None:
    """CAST(x AS TIMESTAMP_NTZ(3)) -> 'YYYY-MM-DD HH24:MI:SS.FF3' (sub-ms digits truncated)."""
    if value is None:
        return None
    d = _to_datetime(value)
    return d.strftime("%Y-%m-%d %H:%M:%S.") + f"{d.microsecond // 1000:03d}"


def canon_date(value: Any) -> str | None:
    """CAST(x AS DATE) -> 'YYYY-MM-DD' (a timestamp is truncated to its date)."""
    return None if value is None else _to_datetime(value).strftime("%Y-%m-%d")


def canon_str(value: Any) -> str | None:
    """CAST(x AS VARCHAR) as-is: no trimming, no case folding, '' stays '' (only NULL is NULL)."""
    return None if value is None else str(value)


CANON: dict[str, Callable[[Any], str | None]] = {
    "int": canon_int, "dec4": canon_dec4, "date": canon_date, "ts": canon_ts, "str": canon_str,
}


def canon_row(values: Sequence[Any], kinds: Sequence[str]) -> str:
    parts = []
    for v, k in zip(values, kinds, strict=True):
        c = CANON[k](v)
        parts.append(NULL_MARKER if c is None else c)
    return FIELD_SEP.join(parts)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def multiset_checksum(row_hashes: Iterable[str]) -> str:
    """Order-independent table checksum: SHA-256 of the sorted row hashes joined by ','."""
    return sha256_hex(HASH_SEP.join(sorted(row_hashes)))


def sample_threshold(rate: float) -> str:
    """Rows whose key hash starts with a 4-hex prefix below this threshold are in the sample."""
    if not 0 < rate < 1:
        raise ValueError("sample rate must be in (0, 1)")
    return format(int(rate * 0x10000), "04x")


def in_sample(key_hash: str, threshold: str) -> bool:
    return key_hash[:4] < threshold


# --------------------------------------------------------------------------------------------
# Check specifications
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Col:
    name: str  # Snowflake column (unquoted -> upper-case) or positional $n for procedure output
    kind: str


@dataclass
class Spec:
    name: str  # check name / report key, e.g. RAW.STATE
    sql_name: str  # path stem under sql/, e.g. raw/state
    cols: list[Col]
    key: list[str]
    sums: list[str]
    relation: str = ""  # FROM clause target; default = name
    distinct: bool = False  # compare SELECT DISTINCT of the checked columns (tables that may re-append)
    call: str | None = None  # CALL statement run before RESULT_SCAN (procedures)
    full_checksum: bool = True
    sample_rate: float | None = None
    expected_from: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        names = [c.name for c in self.cols]
        assert all(c.kind in KINDS for c in self.cols), self.name
        assert set(self.key) <= set(names) and set(self.sums) <= set(names), self.name
        assert self.full_checksum or self.sample_rate, self.name
        if not self.relation:
            self.relation = self.name

    @property
    def kinds(self) -> list[str]:
        return [c.kind for c in self.cols]

    def idx(self, name: str) -> int:
        return [c.name for c in self.cols].index(name)


def _c(spec: str) -> list[Col]:
    out = []
    for item in spec.split():
        name, kind = item.split(":")
        out.append(Col(name, kind))
    return out


TXN_COLS = "TRANSACTION_ID:int ACCOUNT_ID:int TRANSACTION_DATE:ts AMOUNT:dec4 TRANSACTION_TYPE:str BRANCH_ID:int"
TXN_SUMS = ["TRANSACTION_ID", "ACCOUNT_ID", "AMOUNT", "BRANCH_ID"]

RAW_SPECS = [
    Spec("RAW.STATE", "raw/state", _c("STATE_ID:int STATE_NAME:str"), ["STATE_ID"], ["STATE_ID"],
         expected_from="data_sources/sample_db_extract/state.csv"),
    Spec("RAW.CITY", "raw/city", _c("CITY_ID:int CITY_NAME:str STATE_ID:int"), ["CITY_ID"],
         ["CITY_ID", "STATE_ID"], expected_from="data_sources/sample_db_extract/city.csv"),
    Spec("RAW.CUSTOMER", "raw/customer",
         _c("CUSTOMER_ID:int CUSTOMER_NAME:str ADDRESS:str CITY_ID:int AGE:str GENDER:str EMAIL:str"),
         ["CUSTOMER_ID"], ["CUSTOMER_ID", "CITY_ID"],
         expected_from="data_sources/sample_db_extract/customer.csv"),
    Spec("RAW.ACCOUNT", "raw/account",
         _c("ACCOUNT_ID:int CUSTOMER_ID:int ACCOUNT_TYPE:str BALANCE:dec4 DATE_OPENED:ts STATUS:str"),
         ["ACCOUNT_ID"], ["ACCOUNT_ID", "CUSTOMER_ID", "BALANCE"],
         expected_from="data_sources/sample_db_extract/account.csv"),
    Spec("RAW.BRANCH", "raw/branch", _c("BRANCH_ID:int BRANCH_NAME:str BRANCH_LOCATION:str"), ["BRANCH_ID"],
         ["BRANCH_ID"], expected_from="data_sources/sample_db_extract/branch.csv"),
    Spec("RAW.TRANSACTION_DB", "raw/transaction_db", _c(TXN_COLS), ["TRANSACTION_ID"], TXN_SUMS,
         expected_from="data_sources/sample_db_extract/transaction_db.csv"),
    Spec("RAW.TRANSACTION_EXCEL", "raw/transaction_excel", _c(TXN_COLS), ["TRANSACTION_ID"], TXN_SUMS,
         expected_from="data_sources/transaction_excel.xlsx"),
    Spec("RAW.TRANSACTION_CSV", "raw/transaction_csv", _c(TXN_COLS), ["TRANSACTION_ID"], TXN_SUMS,
         expected_from="data_sources/transaction_csv.csv"),
]

DWH_SPECS = [
    Spec("DWH.DIMBRANCH", "dwh/dimbranch", _c("BRANCHID:int BRANCHNAME:str BRANCHLOCATION:str"), ["BRANCHID"],
         ["BRANCHID"], expected_from="migration/baseline/DimBranch.csv"),
    Spec("DWH.DIMACCOUNT", "dwh/dimaccount",
         _c("ACCOUNTID:int CUSTOMERID:int ACCOUNTTYPE:str BALANCE:dec4 DATEOPENED:date STATUS:str"),
         ["ACCOUNTID"], ["ACCOUNTID", "CUSTOMERID", "BALANCE"], expected_from="migration/baseline/DimAccount.csv"),
    Spec("DWH.DIMCUSTOMER", "dwh/dimcustomer",
         _c("CUSTOMERID:int CUSTOMERNAME:str ADDRESS:str CITYNAME:str STATENAME:str AGE:int GENDER:str EMAIL:str"),
         ["CUSTOMERID"], ["CUSTOMERID"], expected_from="migration/baseline/DimCustomer.csv"),
    # SourceSystem / LoadedAt are lineage columns (PLAN.md 3) and deliberately not selected.
    # Full checksum is cheap at this size; the hash-bucket sample is what scales to a production fact.
    Spec("DWH.FACTTRANSACTION", "dwh/facttransaction",
         _c("TRANSACTIONID:int ACCOUNTID:int TRANSACTIONDATE:ts AMOUNT:dec4 TRANSACTIONTYPE:str BRANCHID:int"),
         ["TRANSACTIONID"], ["TRANSACTIONID", "ACCOUNTID", "AMOUNT", "BRANCHID"], sample_rate=0.5,
         expected_from="migration/baseline/FactTransaction.csv"),
]

REJECT_COLS = _c(TXN_COLS + " SOURCESYSTEM:str REJECTREASON:str")
OPS_SPECS = [
    # DISTINCT over the non-timestamp columns so re-runs that append the same rejects again
    # (different REJECTEDAT) do not count twice.
    Spec("OPS.FACTTRANSACTION_REJECTS", "ops/facttransaction_rejects", REJECT_COLS,
         ["TRANSACTION_ID", "SOURCESYSTEM", "REJECTREASON"], TXN_SUMS,
         distinct=True,
         expected_from="Talend rules (PLAN.md 1.6) applied to the 3 transaction sources"),
]


@dataclass(frozen=True)
class ProcCase:
    baseline_file: str
    proc: str
    args: tuple[str, ...]


PROC_CASES = [
    ProcCase("sp_DailyTransaction__2022-01-01__2024-12-31.csv", "DWH.SP_DAILYTRANSACTION",
             ("2022-01-01", "2024-12-31")),
    ProcCase("sp_DailyTransaction__2024-01-18__2024-01-20.csv", "DWH.SP_DAILYTRANSACTION",
             ("2024-01-18", "2024-01-20")),
    ProcCase("sp_BalancePerCustomer__shelly.csv", "DWH.SP_BALANCEPERCUSTOMER", ("shelly",)),
    # 'all' = empty pattern: LIKE '%' + '' + '%' matches every customer (NULL would match none).
    ProcCase("sp_BalancePerCustomer__all.csv", "DWH.SP_BALANCEPERCUSTOMER", ("",)),
]


def _proc_spec(case: ProcCase) -> Spec:
    stem = case.baseline_file[:-4].lower()
    if case.proc == "DWH.SP_DAILYTRANSACTION":
        cols, key, sums = _c("$1:date $2:int $3:dec4"), ["$1"], ["$2", "$3"]
        call = f"CALL {case.proc.lower()}(CAST('{case.args[0]}' AS DATE), CAST('{case.args[1]}' AS DATE));"
    else:
        cols, key, sums = _c("$1:str $2:str $3:dec4 $4:dec4"), ["$1", "$2"], ["$3", "$4"]
        call = f"CALL {case.proc.lower()}('{case.args[0]}');"
    return Spec(f"{case.proc}({', '.join(repr(a) for a in case.args)})", f"procedures/{stem}", cols, key, sums,
                relation="TABLE(RESULT_SCAN(LAST_QUERY_ID()))", call=call,
                expected_from=f"migration/baseline/{case.baseline_file}")


PROC_SPECS = [_proc_spec(c) for c in PROC_CASES]
ALL_SPECS = RAW_SPECS + DWH_SPECS + OPS_SPECS + PROC_SPECS


# --------------------------------------------------------------------------------------------
# SQL generation (committed under sql/, linted by sqlfluff, executed verbatim in live mode)
# --------------------------------------------------------------------------------------------
MAX_SQL_LINE = 120


def _alias(col: str) -> str:
    return ("p" + col[1:] if col.startswith("$") else col).lower()


def _ident(name: str) -> str:
    """Unquoted identifiers are written lower-case (sqlfluff CP02); Snowflake resolves them upper-case."""
    return name if name.startswith("$") else name.lower()


def _canon_expr(col: Col) -> str:
    n = _ident(col.name)
    expr = {
        "int": f"TO_VARCHAR(CAST({n} AS NUMBER(38, 0)))",
        "dec4": f"TO_VARCHAR(CAST({n} AS NUMBER(38, 4)))",
        "date": f"TO_VARCHAR(CAST({n} AS DATE), 'YYYY-MM-DD')",
        "ts": f"TO_VARCHAR(CAST({n} AS TIMESTAMP_NTZ(3)), 'YYYY-MM-DD HH24:MI:SS.FF3')",
        "str": f"CAST({n} AS VARCHAR)",
    }[col.kind]
    return f"COALESCE({expr}, CHR(30))"


def _item(expr: str | list[str], alias: str, indent: int, last: bool) -> list[str]:
    """One SELECT item; ``expr`` may already be multi-line (list, first line unindented)."""
    pad, comma = " " * indent, "" if last else ","
    if isinstance(expr, list):
        return [pad + expr[0]] + expr[1:-1] + [f"{expr[-1]} AS {alias}{comma}"]
    line = f"{pad}{expr} AS {alias}{comma}"
    if len(line) <= MAX_SQL_LINE:
        return [line]
    return [pad + expr, f"{pad}    AS {alias}{comma}"]


def _call(fname: str, args: list[str | list[str]], indent: int) -> list[str]:
    """``fname(args)`` on one line if it fits, else one argument per line (as sqlfluff reflows it)."""
    flat = all(isinstance(a, str) for a in args)
    one = f"{fname}({', '.join(args)})" if flat else ""
    if flat and indent + len(one) + 24 <= MAX_SQL_LINE:
        return [one]
    inner = " " * (indent + 4)
    if flat and len(inner) + len(", ".join(args)) <= MAX_SQL_LINE:
        return [f"{fname}(", inner + ", ".join(args), " " * indent + ")"]
    lines = [f"{fname}("]
    for i, a in enumerate(args):
        comma = "," if i < len(args) - 1 else ""
        if isinstance(a, list):
            lines += [inner + a[0]] + a[1:-1] + [a[-1] + comma]
        else:
            lines.append(inner + a + comma)
    lines.append(" " * indent + ")")
    return lines


def _concat(names: Iterable[str], indent: int) -> list[str]:
    return _call("CONCAT_WS", ["CHR(31)"] + [f"c_{_alias(n)}" for n in names], indent)


def _sha2(names: Iterable[str], indent: int) -> str | list[str]:
    inner = _concat(names, indent + 4)
    if len(inner) == 1:
        one = f"SHA2({inner[0]}, 256)"
        if indent + len(one) + 24 <= MAX_SQL_LINE:
            return one
    return _call("SHA2", [inner if len(inner) > 1 else inner[0], "256"], indent)


def _flat(x: str | list[str]) -> str | list[str]:
    return x[0] if isinstance(x, list) and len(x) == 1 else x


def _header(spec: Spec, what: str) -> list[str]:
    lines = [
        "-- Generated by migration/validation/validate.py --write-sql. Do not edit by hand.",
        f"-- {what}: {spec.name}",
        f"-- Expected values: {spec.expected_from}",
        "-- Canonicalisation: migration/validation/README.md",
    ]
    if spec.call:
        lines.append(spec.call)
        lines.append("")
    return lines


def _canon_cte(spec: Spec, with_sums: bool) -> list[str]:
    items: list[tuple[str, str]] = [(_canon_expr(c), f"c_{_alias(c.name)}") for c in spec.cols]
    if with_sums:
        items += [(f"CAST({_ident(s)} AS NUMBER(38, 4))", f"s_{_alias(s)}") for s in spec.sums]
    relation = spec.relation if spec.call else spec.relation.lower()
    lines = []
    if spec.distinct:
        lines += ["WITH src AS (", "    SELECT DISTINCT"]
        lines += [f"        {_ident(c.name)}{',' if i < len(spec.cols) - 1 else ''}" for i, c in enumerate(spec.cols)]
        lines += [f"    FROM {relation}", "),", ""]
        relation = "src"
    lines += ["canon AS (" if lines else "WITH canon AS (", "    SELECT"]
    for i, (e, a) in enumerate(items):
        lines += _item(e, a, 8, i == len(items) - 1)
    lines += [f"    FROM {relation}", "),", "", "hashed AS (", "    SELECT"]
    return lines


def _hash_items(spec: Spec, with_key: bool) -> list[tuple[str | list[str], str]]:
    items: list[tuple[str | list[str], str]] = []
    if with_key:
        items.append((_flat(_concat(spec.key, 8)), "row_key"))
    items.append((_sha2([c.name for c in spec.cols], 8), "row_hash"))
    items.append((_sha2(spec.key, 8), "key_hash"))
    return items


def render_metrics_sql(spec: Spec) -> str:
    lines = _header(spec, "Row count, key sums and checksums")
    lines += _canon_cte(spec, with_sums=True)
    items: list[tuple[str | list[str], str]] = [(f"s_{_alias(s)}", "") for s in spec.sums]
    items += _hash_items(spec, with_key=False)
    for i, (e, a) in enumerate(items):
        last = i == len(items) - 1
        lines += [f"        {e}{'' if last else ','}"] if not a else _item(e, a, 8, last)
    lines += ["    FROM canon", ")", "", "SELECT"]
    out: list[tuple[str | list[str], str]] = [("COUNT(*)", "row_count")]
    out += [(f"COALESCE(SUM(s_{_alias(s)}), 0)", f"sum_{_alias(s)}") for s in spec.sums]
    if spec.full_checksum:
        out.append(("SHA2(COALESCE(LISTAGG(row_hash, ',') WITHIN GROUP (ORDER BY row_hash), ''), 256)", "checksum"))
    if spec.sample_rate:
        thr = sample_threshold(spec.sample_rate)
        out.append((f"COUNT_IF(LEFT(key_hash, 4) < '{thr}')", "sample_rows"))
        agg = (f"COALESCE(LISTAGG(IFF(LEFT(key_hash, 4) < '{thr}', row_hash, NULL), ',')"
               " WITHIN GROUP (ORDER BY row_hash), '')")
        out.append((["SHA2(", f"        {agg},", "        256", "    )"], "sample_checksum"))
    for i, (e, a) in enumerate(out):
        lines += _item(e, a, 4, i == len(out) - 1)
    lines += ["FROM hashed;", ""]
    return "\n".join(lines)


def render_rowhashes_sql(spec: Spec) -> str:
    lines = _header(spec, "Per-row key and hash (diagnostics on checksum mismatch)")
    lines += _canon_cte(spec, with_sums=False)
    items = _hash_items(spec, with_key=True)
    for i, (e, a) in enumerate(items):
        lines += _item(e, a, 8, i == len(items) - 1)
    lines += ["    FROM canon", ")", "", "SELECT", "    row_key,", "    row_hash", "FROM hashed"]
    if not spec.full_checksum:
        lines.append(f"WHERE LEFT(key_hash, 4) < '{sample_threshold(spec.sample_rate)}'")
    lines += ["ORDER BY row_key, row_hash;", ""]
    return "\n".join(lines)


def sql_paths(spec: Spec, sql_dir: Path = SQL_DIR) -> tuple[Path, Path]:
    return sql_dir / f"{spec.sql_name}.metrics.sql", sql_dir / f"{spec.sql_name}.rowhashes.sql"


def write_sql(sql_dir: Path = SQL_DIR) -> list[Path]:
    written = []
    for spec in ALL_SPECS:
        for path, text in zip(sql_paths(spec, sql_dir), (render_metrics_sql(spec), render_rowhashes_sql(spec))):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written.append(path)
    return written


def stale_sql(sql_dir: Path = SQL_DIR) -> list[str]:
    problems = []
    for spec in ALL_SPECS:
        for path, text in zip(sql_paths(spec, sql_dir), (render_metrics_sql(spec), render_rowhashes_sql(spec))):
            if not path.exists():
                problems.append(f"missing {path.relative_to(sql_dir.parent)}")
            elif path.read_text(encoding="utf-8") != text:
                problems.append(f"stale {path.relative_to(sql_dir.parent)} (run validate.py --write-sql)")
    return problems


# --------------------------------------------------------------------------------------------
# Source readers
# --------------------------------------------------------------------------------------------
Row = tuple


def read_csv(path: Path, expected_header: Sequence[str], encoding: str = "utf-8",
             ts_formats: dict[int, str] | None = None) -> list[Row]:
    """RFC 4180 CSV, header row, empty field = NULL. ``ts_formats`` parses given columns with strptime."""
    with path.open(newline="", encoding=encoding) as fh:
        reader = csv.reader(fh)
        header = next(reader)
        if [h.strip().upper() for h in header] != [h.upper() for h in expected_header]:
            raise ValueError(f"{path}: header {header} != {list(expected_header)}")
        rows = []
        for rec in reader:
            if not rec:
                continue
            vals: list[Any] = [None if v == "" else v for v in rec]
            for i, fmt in (ts_formats or {}).items():
                if vals[i] is not None:
                    vals[i] = dt.datetime.strptime(vals[i], fmt)
            rows.append(tuple(vals))
    return rows


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_EXCEL_EPOCH = dt.datetime(1899, 12, 30)


def excel_serial_to_datetime(serial: float) -> dt.datetime:
    """Excel 1900 date system serial -> datetime, rounded to the millisecond (floating-point noise)."""
    ms = round(float(serial) * 86_400_000)
    return _EXCEL_EPOCH + dt.timedelta(milliseconds=ms)


def read_xlsx(path: Path, expected_header: Sequence[str], date_cols: Iterable[int],
              sheet: str = "Sheet1") -> list[Row]:
    """Minimal stdlib .xlsx reader (shared strings, inline strings, numbers) so offline mode needs no deps."""
    with zipfile.ZipFile(path) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
        wb = ElementTree.fromstring(zf.read("xl/workbook.xml"))
        names = [s.get("name") for s in wb.findall("m:sheets/m:sheet", _NS)]
        sheet_xml = zf.read(f"xl/worksheets/sheet{names.index(sheet) + 1}.xml")
    root = ElementTree.fromstring(sheet_xml)
    grid: list[list[Any]] = []
    for row in root.findall("m:sheetData/m:row", _NS):
        vals: dict[int, Any] = {}
        for c in row.findall("m:c", _NS):
            col = 0
            for ch in re.match(r"[A-Z]+", c.get("r")).group(0):
                col = col * 26 + ord(ch) - 64
            t, v = c.get("t"), c.find("m:v", _NS)
            if t == "s":
                vals[col - 1] = shared[int(v.text)]
            elif t == "inlineStr":
                vals[col - 1] = "".join(x.text or "" for x in c.iter(f"{{{_NS['m']}}}t"))
            elif v is not None and v.text is not None:
                vals[col - 1] = v.text if t == "str" else Decimal(v.text)
        grid.append([vals.get(i) for i in range(max(vals) + 1 if vals else 0)])
    header, body = grid[0], grid[1:]
    if [str(h).upper() for h in header] != [h.upper() for h in expected_header]:
        raise ValueError(f"{path}: header {header} != {list(expected_header)}")
    width = len(expected_header)
    out = []
    for rec in body:
        rec = (rec + [None] * width)[:width]
        if all(v is None for v in rec):
            continue
        for i in date_cols:
            if rec[i] is not None:
                rec[i] = excel_serial_to_datetime(rec[i])
        out.append(tuple(rec))
    return out


@dataclass
class Sources:
    data_dir: Path = DEFAULT_DATA_DIR
    baseline_dir: Path = DEFAULT_BASELINE_DIR

    def _hdr(self, spec_name: str) -> list[str]:
        return [c.name for c in next(s for s in RAW_SPECS if s.name == spec_name).cols]

    def raw(self, spec_name: str) -> list[Row]:
        table = spec_name.split(".")[1].lower()
        hdr = self._hdr(spec_name)
        if table == "transaction_excel":
            return read_xlsx(self.data_dir / "transaction_excel.xlsx", hdr, date_cols=[2])
        if table == "transaction_csv":  # Talend: ISO-8859-15, date pattern dd-MM-yyyy HH:mm:ss
            return read_csv(self.data_dir / "transaction_csv.csv", hdr, encoding="iso8859_15",
                            ts_formats={2: "%d-%m-%Y %H:%M:%S"})
        return read_csv(self.data_dir / "sample_db_extract" / f"{table}.csv", hdr)

    def baseline(self, spec: Spec) -> list[Row]:
        path = REPO_ROOT / spec.expected_from
        if spec.expected_from.startswith("migration/baseline/"):
            path = self.baseline_dir / Path(spec.expected_from).name
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            header = next(reader)
            if len(header) != len(spec.cols):
                raise ValueError(f"{path}: {len(header)} columns, spec has {len(spec.cols)}")
            if not spec.call and [h.upper() for h in header] != [c.name for c in spec.cols]:
                raise ValueError(f"{path}: header {header} != {[c.name for c in spec.cols]}")
            return [tuple(None if v == "" else v for v in rec) for rec in reader if rec]


# --------------------------------------------------------------------------------------------
# Talend job emulation (PLAN.md 1.5 / 1.6) and procedure logic, in Python
# --------------------------------------------------------------------------------------------
SOURCE_RANK = {"DB": 1, "EXCEL": 2, "CSV": 3}


def _upper(v: Any) -> Any:
    return None if v is None else str(v).upper()


def talend_dim_branch(src: Sources) -> list[Row]:
    return [tuple(r) for r in src.raw("RAW.BRANCH")]


def talend_dim_account(src: Sources) -> list[Row]:
    return [(a, c, t, bal, None if d is None else _to_datetime(d).date(), s)
            for a, c, t, bal, d, s in src.raw("RAW.ACCOUNT")]


def talend_dim_customer(src: Sources) -> list[Row]:
    states = {r[0]: r[1] for r in src.raw("RAW.STATE")}
    cities = {r[0]: (r[1], r[2]) for r in src.raw("RAW.CITY")}
    out = []
    for cid, name, addr, city_id, age, gender, email in src.raw("RAW.CUSTOMER"):
        city_name, state_id = cities.get(city_id, (None, None))  # LEFT OUTER JOIN city
        out.append((cid, _upper(name), _upper(addr), city_name, states.get(state_id),  # LEFT OUTER JOIN state
                    None if age is None else int(str(age).strip()), _upper(gender), email))
    return out


def talend_fact(src: Sources) -> tuple[list[Row], list[Row]]:
    """tUnite(DB, EXCEL, CSV) -> tUniqRow(transaction_id, keep highest precedence) -> FK filter.

    Returns (fact rows, reject rows). Rejects carry the 6 source columns + SourceSystem + RejectReason.
    """
    union = []
    for system, table in (("DB", "RAW.TRANSACTION_DB"), ("EXCEL", "RAW.TRANSACTION_EXCEL"),
                          ("CSV", "RAW.TRANSACTION_CSV")):
        for pos, r in enumerate(src.raw(table)):
            union.append((SOURCE_RANK[system], pos, system, r))
    union.sort(key=lambda u: (int(u[3][0]), u[0], u[1]))
    accounts = {int(r[0]) for r in talend_dim_account(src)}
    branches = {int(r[0]) for r in talend_dim_branch(src)}
    fact, rejects, seen = [], [], set()
    for _, _, system, r in union:
        tid = int(r[0])
        if tid in seen:
            rejects.append(tuple(r) + (system, "DUPLICATE_LOWER_PRECEDENCE"))
            continue
        seen.add(tid)
        if r[1] is None or int(r[1]) not in accounts:
            rejects.append(tuple(r) + (system, "ORPHAN_ACCOUNT"))
        elif r[5] is None or int(r[5]) not in branches:
            rejects.append(tuple(r) + (system, "ORPHAN_BRANCH"))
        else:
            fact.append(tuple(r))
    return fact, rejects


def proc_daily_transaction(fact: list[Row], start: str, end: str) -> list[Row]:
    lo, hi = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    agg: dict[dt.date, list[Decimal]] = {}
    for r in fact:
        d = _to_datetime(r[2]).date()
        if lo <= d <= hi:
            a = agg.setdefault(d, [Decimal(0), Decimal(0)])
            a[0] += 1
            a[1] += _to_decimal(r[3])
    return [(d, int(c), s) for d, (c, s) in sorted(agg.items())]


def proc_balance_per_customer(customers: list[Row], accounts: list[Row], fact: list[Row],
                              pattern: str) -> list[Row]:
    net: dict[int, Decimal] = {}
    for r in fact:
        amt = _to_decimal(r[3])
        net[int(r[1])] = net.get(int(r[1]), Decimal(0)) + (amt if r[4] == "Deposit" else -amt)
    out = []
    for c in customers:
        if c[1] is None or pattern.lower() not in c[1].lower():  # ILIKE '%' || pattern || '%'
            continue
        for a in accounts:
            if int(a[1]) == int(c[0]) and a[5] == "active":
                bal = _to_decimal(a[3])
                out.append((c[1], a[2], bal, bal + net.get(int(a[0]), Decimal(0))))
    return out


def expected_transformed(src: Sources) -> dict[str, list[Row]]:
    """Every check's rows as produced by the Talend rules + procedure logic from the source extracts."""
    dims = {
        "DWH.DIMBRANCH": talend_dim_branch(src),
        "DWH.DIMACCOUNT": talend_dim_account(src),
        "DWH.DIMCUSTOMER": talend_dim_customer(src),
    }
    fact, rejects = talend_fact(src)
    out = {**dims, "DWH.FACTTRANSACTION": fact, "OPS.FACTTRANSACTION_REJECTS": rejects}
    for case, spec in zip(PROC_CASES, PROC_SPECS):
        if case.proc == "DWH.SP_DAILYTRANSACTION":
            out[spec.name] = proc_daily_transaction(fact, *case.args)
        else:
            out[spec.name] = proc_balance_per_customer(dims["DWH.DIMCUSTOMER"], dims["DWH.DIMACCOUNT"], fact,
                                                       case.args[0])
    for spec in RAW_SPECS:
        out[spec.name] = src.raw(spec.name)
    return out


def expected_live(src: Sources) -> dict[str, list[Row]]:
    """What Snowflake must contain: RAW = source files, DWH/procs = SQL Server baseline, OPS = Talend rules."""
    out = {s.name: src.raw(s.name) for s in RAW_SPECS}
    for s in DWH_SPECS + PROC_SPECS:
        out[s.name] = src.baseline(s)
    out["OPS.FACTTRANSACTION_REJECTS"] = talend_fact(src)[1]
    return out


# --------------------------------------------------------------------------------------------
# Metrics and comparison
# --------------------------------------------------------------------------------------------
def row_hashes(spec: Spec, rows: Iterable[Row]) -> list[tuple[str, str]]:
    """[(canonical key, row hash)] in the same form the rowhashes SQL returns."""
    key_idx = [spec.idx(k) for k in spec.key]
    key_kinds = [spec.kinds[i] for i in key_idx]
    out = []
    for r in rows:
        key = canon_row([r[i] for i in key_idx], key_kinds)
        out.append((key, sha256_hex(canon_row(r, spec.kinds))))
    return out


def compute_metrics(spec: Spec, rows: list[Row]) -> dict[str, str]:
    m: dict[str, str] = {"ROW_COUNT": str(len(rows))}
    for s in spec.sums:
        i = spec.idx(s)
        total = sum((Decimal(canon_dec4(r[i])) for r in rows if r[i] is not None), Decimal(0))
        m[f"SUM_{_alias(s).upper()}"] = canon_dec4(total)
    pairs = row_hashes(spec, rows)
    if spec.full_checksum:
        m["CHECKSUM"] = multiset_checksum(h for _, h in pairs)
    if spec.sample_rate:
        thr = sample_threshold(spec.sample_rate)
        sampled = [h for k, h in pairs if in_sample(sha256_hex(k), thr)]
        m["SAMPLE_ROWS"] = str(len(sampled))
        m["SAMPLE_CHECKSUM"] = multiset_checksum(sampled)
    return m


def normalise_actual(raw: dict[str, Any]) -> dict[str, str]:
    """Snowflake metric row -> the string form compute_metrics produces."""
    out = {}
    for k, v in raw.items():
        k = k.upper()
        if k.startswith("SUM_"):
            out[k] = canon_dec4(0 if v is None else v)
        elif k in ("ROW_COUNT", "SAMPLE_ROWS"):
            out[k] = canon_int(v)
        else:
            out[k] = None if v is None else str(v).lower()
    return out


def diff_rows(expected: list[tuple[str, str]], actual: list[tuple[str, str]], limit: int = 20) -> dict:
    def human(key: str) -> str:
        return "|".join("NULL" if p == NULL_MARKER else p for p in key.split(FIELD_SEP))

    exp_c, act_c = Counter(expected), Counter(actual)
    missing = exp_c - act_c
    extra = act_c - exp_c
    missing_keys = Counter(k for k, _ in missing.elements())
    extra_keys = Counter(k for k, _ in extra.elements())
    changed = sorted(set(missing_keys) & set(extra_keys))
    return {
        "missing_in_actual": [human(k) for k in sorted(set(missing_keys) - set(changed))][:limit],
        "unexpected_in_actual": [human(k) for k in sorted(set(extra_keys) - set(changed))][:limit],
        "changed": [human(k) for k in changed][:limit],
    }


@dataclass
class CheckResult:
    name: str
    expected_from: str
    actual_from: str
    metrics: list[dict[str, Any]] = field(default_factory=list)
    row_diff: dict | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and all(m["ok"] for m in self.metrics)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "expected_from": self.expected_from,
                "actual_from": self.actual_from, "metrics": self.metrics, "row_diff": self.row_diff,
                "error": self.error}


def compare(spec: Spec, expected: dict[str, str], actual: dict[str, str], actual_from: str) -> CheckResult:
    res = CheckResult(spec.name, spec.expected_from, actual_from)
    for k in expected:
        a = actual.get(k)
        res.metrics.append({"metric": k, "expected": expected[k], "actual": a, "ok": a == expected[k]})
    return res


# --------------------------------------------------------------------------------------------
# Offline mode
# --------------------------------------------------------------------------------------------
OFFLINE_SPECS = DWH_SPECS + PROC_SPECS


def run_offline(src: Sources) -> list[CheckResult]:
    results = []
    stale = stale_sql()
    sql_check = CheckResult("SQL files in sync with generator", "validate.py specs", "migration/validation/sql/")
    sql_check.metrics.append({"metric": "STALE_OR_MISSING", "expected": "0", "actual": str(len(stale)),
                              "ok": not stale})
    if stale:
        sql_check.row_diff = {"files": stale}
    results.append(sql_check)
    try:
        computed = expected_transformed(src)
    except Exception as exc:  # noqa: BLE001 - reported as a failed check
        r = CheckResult("Talend emulation", "data_sources/", "-", error=f"{type(exc).__name__}: {exc}")
        return results + [r]
    for spec in OFFLINE_SPECS:
        actual_from = spec.expected_from
        res = CheckResult(spec.name, "Talend rules applied to data_sources/", actual_from)
        try:
            base = src.baseline(spec)
            res = compare(spec, compute_metrics(spec, computed[spec.name]), compute_metrics(spec, base), actual_from)
            res.expected_from = "Talend rules applied to data_sources/"
            if not res.ok:
                res.row_diff = diff_rows(row_hashes(spec, computed[spec.name]), row_hashes(spec, base))
        except Exception as exc:  # noqa: BLE001
            res.error = f"{type(exc).__name__}: {exc}"
        results.append(res)
    # Rejects have no SQL Server table (FK violations were dropped silently); check the invariants instead.
    rej = computed["OPS.FACTTRANSACTION_REJECTS"]
    res = CheckResult("OPS.FACTTRANSACTION_REJECTS", "PLAN.md 1.6", "Talend rules applied to data_sources/")
    orphan = sorted(int(r[0]) for r in rej if r[7].startswith("ORPHAN"))
    dups = sorted((int(r[0]), r[6]) for r in rej if r[7] == "DUPLICATE_LOWER_PRECEDENCE")
    union = sum(len(computed[s.name]) for s in RAW_SPECS if s.name.startswith("RAW.TRANSACTION"))
    for metric, exp, act in (
        ("ORPHAN_TRANSACTION_IDS", "[23, 24, 25]", str(orphan)),
        ("DUPLICATE_LOWER_PRECEDENCE", "[(6, 'EXCEL'), (7, 'EXCEL'), (14, 'CSV'), (15, 'CSV')]", str(dups)),
        ("UNION_EQUALS_FACT_PLUS_REJECTS", str(union), str(len(computed["DWH.FACTTRANSACTION"]) + len(rej))),
    ):
        res.metrics.append({"metric": metric, "expected": exp, "actual": act, "ok": exp == act})
    results.append(res)
    # RAW row counts must match the PLAN.md 1.1 inventory.
    plan_counts = {"RAW.STATE": 9, "RAW.CITY": 52, "RAW.CUSTOMER": 20, "RAW.ACCOUNT": 21, "RAW.BRANCH": 5,
                   "RAW.TRANSACTION_DB": 10, "RAW.TRANSACTION_EXCEL": 7, "RAW.TRANSACTION_CSV": 12}
    for spec in RAW_SPECS:
        res = CheckResult(spec.name, "PLAN.md 1.1 row count", spec.expected_from)
        res.metrics.append({"metric": "ROW_COUNT", "expected": str(plan_counts[spec.name]),
                            "actual": str(len(computed[spec.name])),
                            "ok": plan_counts[spec.name] == len(computed[spec.name])})
        results.append(res)
    return results


# --------------------------------------------------------------------------------------------
# Live mode
# --------------------------------------------------------------------------------------------
ENV = {
    "account": "SNOWFLAKE_ACCOUNT", "user": "SNOWFLAKE_USER", "password": "SNOWFLAKE_PASSWORD",
    "private_key_path": "SNOWFLAKE_PRIVATE_KEY_PATH", "private_key_passphrase": "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE",
    "role": "SNOWFLAKE_ROLE", "warehouse": "SNOWFLAKE_WAREHOUSE", "database": "SNOWFLAKE_DATABASE",
}


class ConfigError(Exception):
    pass


def connection_params(args: argparse.Namespace) -> dict[str, Any]:
    p: dict[str, Any] = {
        "account": args.account, "user": args.user, "role": args.role, "warehouse": args.warehouse,
        "database": args.database or "BANKING_DWH", "session_parameters": {"QUERY_TAG": "migration-validation"},
        "application": "banking-etl-warehouse-validation",
    }
    if not p["user"]:
        raise ConfigError("SNOWFLAKE_USER (or --user) is required")
    if args.private_key_path:
        from cryptography.hazmat.primitives import serialization  # lazy: dependency of the connector

        pw = os.environ.get(ENV["private_key_passphrase"])
        key = serialization.load_pem_private_key(Path(args.private_key_path).read_bytes(),
                                                 password=pw.encode() if pw else None)
        p["private_key"] = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption())
    elif os.environ.get(ENV["password"]):
        p["password"] = os.environ[ENV["password"]]
    else:
        raise ConfigError("set SNOWFLAKE_PASSWORD or SNOWFLAKE_PRIVATE_KEY_PATH (key-pair auth)")
    return {k: v for k, v in p.items() if v is not None}


def connect(params: dict[str, Any]):
    import snowflake.connector  # lazy import: offline mode / CI must not need the connector

    return snowflake.connector.connect(**params)


def _fetch(conn, sql: str) -> tuple[list[str], list[tuple]]:
    cursors = conn.execute_string(sql)
    cur = cursors[-1]
    cols = [d[0] for d in cur.description]
    return cols, cur.fetchall()


def run_live(conn, src: Sources, specs: Sequence[Spec] = ALL_SPECS, sql_dir: Path = SQL_DIR) -> list[CheckResult]:
    expected_rows = expected_live(src)
    results = []
    for spec in specs:
        metrics_path, rowhash_path = sql_paths(spec, sql_dir)
        actual_from = f"Snowflake {spec.relation if not spec.call else spec.call.rstrip(';')}"
        try:
            rows = expected_rows[spec.name]
            cols, data = _fetch(conn, metrics_path.read_text(encoding="utf-8"))
            res = compare(spec, compute_metrics(spec, rows), normalise_actual(dict(zip(cols, data[0]))), actual_from)
            if not res.ok:
                _, hrows = _fetch(conn, rowhash_path.read_text(encoding="utf-8"))
                exp = row_hashes(spec, rows)
                if not spec.full_checksum:
                    thr = sample_threshold(spec.sample_rate)
                    exp = [(k, h) for k, h in exp if in_sample(sha256_hex(k), thr)]
                res.row_diff = diff_rows(exp, [(str(k), str(h).lower()) for k, h in hrows])
        except Exception as exc:  # noqa: BLE001 - one broken object must not hide the others
            res = CheckResult(spec.name, spec.expected_from, actual_from, error=f"{type(exc).__name__}: {exc}")
        results.append(res)
    return results


# --------------------------------------------------------------------------------------------
# Reporting / CLI
# --------------------------------------------------------------------------------------------
def build_report(mode: str, results: list[CheckResult], extra: dict | None = None) -> dict[str, Any]:
    failed = [r.name for r in results if not r.ok]
    return {
        "mode": mode, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "passed": not failed, "summary": {"checks": len(results), "passed": len(results) - len(failed),
                                          "failed": len(failed), "failed_checks": failed},
        **(extra or {}), "checks": [r.as_dict() for r in results],
    }


def render_summary(report: dict[str, Any]) -> str:
    lines = [f"Migration validation ({report['mode']}): "
             f"{report['summary']['passed']}/{report['summary']['checks']} checks passed", ""]
    width = max(len(c["name"]) for c in report["checks"]) if report["checks"] else 10
    for c in report["checks"]:
        rc = next((m for m in c["metrics"] if m["metric"] == "ROW_COUNT"), None)
        rows = f"rows {rc['expected']:>3}/{rc['actual'] or '-':>3}" if rc else " " * 12
        lines.append(f"  {'PASS' if c['ok'] else 'FAIL'}  {c['name']:<{width}}  {rows}")
        if c["error"]:
            lines.append(f"        error: {c['error']}")
        for m in c["metrics"]:
            if not m["ok"]:
                lines.append(f"        {m['metric']}: expected {m['expected']} got {m['actual']}")
        for k, v in (c["row_diff"] or {}).items():
            if v:
                lines.append(f"        {k}: {', '.join(map(str, v))}")
    lines.append("")
    lines.append("RESULT: " + ("PASS" if report["passed"] else "FAIL"))
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="no Snowflake: extracts + Talend rules vs baseline")
    mode.add_argument("--write-sql", action="store_true", help="regenerate migration/validation/sql/ and exit")
    p.add_argument("--require-live", action="store_true", help="exit 2 instead of skipping when not configured")
    p.add_argument("--report", type=Path, help="write the JSON report here (default: stdout only summary)")
    p.add_argument("--json", action="store_true", help="print the JSON report instead of the summary")
    p.add_argument("--only", action="append", default=[], metavar="CHECK",
                   help="live: run only checks whose name starts with this (repeatable), e.g. DWH. or RAW.STATE")
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    for opt, env in (("account", ENV["account"]), ("user", ENV["user"]), ("role", ENV["role"]),
                     ("warehouse", ENV["warehouse"]), ("database", ENV["database"]),
                     ("private-key-path", ENV["private_key_path"])):
        p.add_argument(f"--{opt}", default=os.environ.get(env) or None, help=f"default: ${env}")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.write_sql:
        for path in write_sql():
            print(path.relative_to(REPO_ROOT))
        return 0
    src = Sources(args.data_dir, args.baseline_dir)
    if args.offline:
        report = build_report("offline", run_offline(src))
    else:
        if not args.account:
            msg = "SNOWFLAKE_ACCOUNT is not set: live validation skipped (use --offline for the offline checks)."
            print(msg, file=sys.stderr)
            return 2 if args.require_live else 0
        specs = [s for s in ALL_SPECS if not args.only or any(s.name.startswith(o) for o in args.only)]
        try:
            conn = connect(connection_params(args))
        except Exception as exc:  # noqa: BLE001
            print(f"cannot connect to Snowflake: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        try:
            results = run_live(conn, src, specs)
        finally:
            conn.close()
        report = build_report("live", results, {"account": args.account, "database": args.database or "BANKING_DWH"})
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2) if args.json else render_summary(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
