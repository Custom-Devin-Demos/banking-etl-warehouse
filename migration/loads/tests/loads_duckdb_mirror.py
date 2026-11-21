"""DuckDB mirror of the Unit 3 Snowflake load SQL, for offline tests.

The tests execute the committed Snowflake SQL itself (RAW DDL, the COPY INTO column transforms of
OPS.SP_COPY_RAW, the transform views and the DML inside the OPS.SP_LOAD_* procedures) after a small, explicit
dialect translation, so a change to the SQL is exercised by the tests. Only the procedural shell (DECLARE,
EXCEPTION, RETURN, SYSTEM$ calls), stages and streams are emulated in Python.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from decimal import Decimal
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[3]
LOADS_SQL = REPO_ROOT / "migration" / "snowflake" / "loads"
DATA = REPO_ROOT / "data_sources"
BASELINE = REPO_ROOT / "migration" / "baseline"
RUN_TS = dt.datetime(2026, 1, 1, 2, 0, 0)

SNOWFLAKE_TO_STRPTIME = {
    "YYYY-MM-DD HH24:MI:SS": "%Y-%m-%d %H:%M:%S",
    "DD-MM-YYYY HH24:MI:SS": "%d-%m-%Y %H:%M:%S",
}

# Unit 1 owns this DDL (migration/snowflake/ddl/); mirrored here with PLAN.md §3 names and types.
UNIT1_TABLES = """
CREATE TABLE DWH.DIMBRANCH (BRANCHID INT NOT NULL PRIMARY KEY, BRANCHNAME VARCHAR, BRANCHLOCATION VARCHAR);
CREATE TABLE DWH.DIMACCOUNT (
    ACCOUNTID INT NOT NULL PRIMARY KEY, CUSTOMERID INT, ACCOUNTTYPE VARCHAR, BALANCE DECIMAL(19, 4),
    DATEOPENED DATE, STATUS VARCHAR
);
CREATE TABLE DWH.DIMCUSTOMER (
    CUSTOMERID INT NOT NULL PRIMARY KEY, CUSTOMERNAME VARCHAR, ADDRESS VARCHAR, CITYNAME VARCHAR,
    STATENAME VARCHAR, AGE INT, GENDER VARCHAR, EMAIL VARCHAR
);
CREATE TABLE DWH.FACTTRANSACTION (
    TRANSACTIONID INT NOT NULL PRIMARY KEY, ACCOUNTID INT, TRANSACTIONDATE TIMESTAMP_MS, AMOUNT DECIMAL(19, 4),
    TRANSACTIONTYPE VARCHAR, BRANCHID INT, SOURCESYSTEM VARCHAR, LOADEDAT TIMESTAMP_MS
);
CREATE TABLE OPS.FACTTRANSACTION_REJECTS (
    TRANSACTION_ID INT, ACCOUNT_ID INT, TRANSACTION_DATE TIMESTAMP_MS, AMOUNT DECIMAL(19, 4),
    TRANSACTION_TYPE VARCHAR, BRANCH_ID INT, SOURCESYSTEM VARCHAR, REJECTREASON VARCHAR NOT NULL,
    REJECTEDAT TIMESTAMP NOT NULL
);
CREATE TABLE OPS.ETL_ERROR_LOG (
    PROCEDURE_NAME VARCHAR, SQLCODE BIGINT, SQLSTATE VARCHAR, SQLERRM VARCHAR, CONTEXT VARCHAR
);
"""

STREAM_SOURCES = {
    "RAW.STRM_TRANSACTION_DB": "RAW.TRANSACTION_DB",
    "RAW.STRM_TRANSACTION_EXCEL": "RAW.TRANSACTION_EXCEL",
    "RAW.STRM_TRANSACTION_CSV": "RAW.TRANSACTION_CSV",
}


def strip_comments(sql: str) -> str:
    out, i, in_str = [], 0, False
    while i < len(sql):
        ch = sql[i]
        if in_str:
            out.append(ch)
            if ch == "'":
                in_str = False
        elif ch == "'":
            in_str = True
            out.append(ch)
        elif sql.startswith("--", i):
            while i < len(sql) and sql[i] != "\n":
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def split_statements(sql: str) -> list[str]:
    parts, buf, in_str, in_dollar, i = [], [], False, False, 0
    while i < len(sql):
        ch = sql[i]
        if not in_str and sql.startswith("$$", i):
            in_dollar = not in_dollar
            buf.append("$$")
            i += 2
            continue
        if ch == "'" and not in_dollar:
            in_str = not in_str
        if ch == ";" and not in_str and not in_dollar:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    if "".join(buf).strip():
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def to_duckdb(sql: str) -> str:
    sql = re.sub(r"(?im)^\s*COMMENT\s*=\s*'(?:[^']|'')*'\s*$", "", sql)
    sql = re.sub(r"\bCREATE\s+TRANSIENT\s+TABLE\b", "CREATE TABLE", sql, flags=re.I)
    sql = re.sub(r"\bTIMESTAMP_NTZ\s*\(\s*3\s*\)", "TIMESTAMP_MS", sql, flags=re.I)
    sql = re.sub(r"\bTIMESTAMP_NTZ\s*\(\s*0\s*\)", "TIMESTAMP_S", sql, flags=re.I)
    sql = re.sub(r"\bTIMESTAMP_NTZ\b", "TIMESTAMP", sql, flags=re.I)
    sql = re.sub(r"\bNUMBER\s*\(", "DECIMAL(", sql, flags=re.I)
    sql = re.sub(r"\bNUMBER\b", "BIGINT", sql, flags=re.I)
    sql = re.sub(r"\bSYSDATE\(\)", "CAST(now() AS TIMESTAMP)", sql, flags=re.I)
    sql = re.sub(r"\bEQUAL_NULL\(([\w.]+), ([\w.]+)\)", r"(\1 IS NOT DISTINCT FROM \2)", sql, flags=re.I)
    sql = sql.replace("METADATA$ACTION", "METADATA_ACTION")
    sql = sql.replace(":run_ts", f"TIMESTAMP '{RUN_TS:%Y-%m-%d %H:%M:%S}'")
    return sql


def deploy_statements(filename: str) -> list[str]:
    code = strip_comments((LOADS_SQL / filename).read_text(encoding="utf-8"))
    return [
        to_duckdb(s)
        for s in split_statements(code)
        if not re.match(r"USE\s+DATABASE\b", s, re.I)
    ]


def procedure_dml(filename: str) -> dict[str, list[str]]:
    """DML statements of a procedure body, keyed by the step name assigned just before them."""
    code = strip_comments((LOADS_SQL / filename).read_text(encoding="utf-8"))
    body = code.split("$$")[1]
    body = re.split(r"(?m)^EXCEPTION\s*$", body)[0]
    steps: dict[str, list[str]] = {}
    current = "start"
    for stmt in split_statements(body):
        match = re.search(r"step\s*:=\s*'([^']+)'", stmt)
        if match:
            current = match.group(1)
            stmt = stmt[match.end():].strip()
            if not stmt:
                continue
        if re.match(r"(MERGE|INSERT|DELETE|UPDATE)\b", stmt, re.I):
            overwrite = re.match(r"INSERT\s+OVERWRITE\s+INTO\s+([\w.]+)", stmt, re.I)
            if overwrite:
                steps.setdefault(current, []).append(f"DELETE FROM {overwrite.group(1)}")
                stmt = re.sub(r"INSERT\s+OVERWRITE\s+INTO", "INSERT INTO", stmt, count=1, flags=re.I)
            steps.setdefault(current, []).append(to_duckdb(stmt))
    return steps


def copy_statements() -> dict[str, dict]:
    """COPY INTO statements of OPS.SP_COPY_RAW: target, columns, select expressions, stage path, file format."""
    code = strip_comments((LOADS_SQL / "20_sp_copy_raw.sql").read_text(encoding="utf-8"))
    pattern = re.compile(
        r"COPY\s+INTO\s+(RAW\.\w+)\s*\(([^)]*)\)\s*FROM\s*\(\s*SELECT\s+(.*?)\s+"
        r"FROM\s+@OPS\.LANDING_STAGE/([\w/]+?)/?\s*\)"
        r"\s*FILE_FORMAT\s*=\s*\(\s*FORMAT_NAME\s*=\s*'([\w.]+)'\s*\)",
        re.S | re.I,
    )
    copies = {}
    for target, cols, exprs, path, fmt in pattern.findall(code):
        copies[target.upper()] = {
            "columns": [c.strip() for c in cols.split(",")],
            "exprs": [e.strip() for e in re.split(r",(?![^(]*\))", exprs)],
            "stage_path": path,
            "file_format": fmt,
        }
    return copies


def _copy_expr(expr: str, source_file: str) -> str:
    expr = re.sub(r"\$(\d+)", r"C\1", expr)

    def ts(match):
        return f"strptime({match.group(1)}, '{SNOWFLAKE_TO_STRPTIME[match.group(2)]}')"

    expr = re.sub(r"TO_TIMESTAMP_NTZ\((\w+),\s*'([^']+)'\)", ts, expr, flags=re.I)
    expr = expr.replace("METADATA$FILENAME", "'" + source_file.replace("'", "''") + "'")
    return expr.replace("METADATA$FILE_ROW_NUMBER", "ROW_NUMBER() OVER ()")


def new_connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for schema in ("RAW", "DWH", "OPS"):
        con.execute(f"CREATE SCHEMA {schema}")
    for stmt in split_statements(UNIT1_TABLES):
        con.execute(stmt)
    for filename in ("10_raw_tables.sql", "12_ops_work_tables.sql"):
        for stmt in deploy_statements(filename):
            con.execute(stmt)
    for stream, table in STREAM_SOURCES.items():
        con.execute(f"CREATE TABLE {stream} AS SELECT *, 'INSERT' AS METADATA_ACTION FROM {table} WHERE FALSE")
    for stmt in deploy_statements("30_transform_views.sql"):
        con.execute(stmt)
    return con


def copy_into(con, target: str, csv_path: Path, loaded_at: dt.datetime, source_file: str | None = None) -> int:
    """Run the COPY INTO of OPS.SP_COPY_RAW for one staged file; also feed the table's stream, if any."""
    spec = copy_statements()[target]
    source_file = source_file or f"{spec['stage_path']}/{csv_path.name}.gz"
    with csv_path.open(encoding="utf-8", newline="") as handle:
        width = len(next(csv.reader(handle)))
    names = ", ".join(f"'C{i}'" for i in range(1, width + 1))
    select = ", ".join(_copy_expr(e, source_file) for e in spec["exprs"])
    columns = ", ".join(spec["columns"] + ["_LOADED_AT"])
    sql = (
        f"INSERT INTO {target} ({columns}) SELECT {select}, ? "
        f"FROM read_csv(?, header = true, all_varchar = true, names = [{names}])"
    )
    before = con.execute(f"SELECT COUNT(*) FROM {target}").fetchone()[0]
    con.execute(sql, [loaded_at, str(csv_path)])
    for stream, table in STREAM_SOURCES.items():
        if table == target:
            con.execute(f"INSERT INTO {stream} SELECT *, 'INSERT' FROM {table} WHERE _LOADED_AT = ?", [loaded_at])
    return con.execute(f"SELECT COUNT(*) FROM {target}").fetchone()[0] - before


EXTRACT_FILES = {
    "RAW.STATE": "sample_db_extract/state.csv",
    "RAW.CITY": "sample_db_extract/city.csv",
    "RAW.CUSTOMER": "sample_db_extract/customer.csv",
    "RAW.ACCOUNT": "sample_db_extract/account.csv",
    "RAW.BRANCH": "sample_db_extract/branch.csv",
    "RAW.TRANSACTION_DB": "sample_db_extract/transaction_db.csv",
    "RAW.TRANSACTION_CSV": "transaction_csv.csv",
}


def source_file_for(target: str, excel_csv: Path) -> Path:
    return excel_csv if target == "RAW.TRANSACTION_EXCEL" else DATA / EXTRACT_FILES[target]


def run_steps(con, steps: dict[str, list[str]], names: list[str] | None = None) -> None:
    for name in names or list(steps):
        for stmt in steps[name]:
            con.execute(stmt)


def load_dims(con) -> None:
    for filename in ("31_sp_load_dimbranch.sql", "32_sp_load_dimaccount.sql", "33_sp_load_dimcustomer.sql"):
        run_steps(con, procedure_dml(filename))


def run_full_fact(con) -> None:
    run_steps(con, procedure_dml("34_sp_load_facttransaction.sql"))
    for stream in STREAM_SOURCES:
        con.execute(f"DELETE FROM {stream}")


def run_incremental_fact(con) -> None:
    """OPS.SP_LOAD_FACTTRANSACTION_INCREMENTAL; COMMIT advances the stream offsets (emulated by emptying them)."""
    run_steps(con, procedure_dml("35_sp_load_facttransaction_incremental.sql"))
    for stream in STREAM_SOURCES:
        con.execute(f"DELETE FROM {stream}")


def read_baseline(name: str) -> list[dict[str, str]]:
    with (BASELINE / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def canonical(value) -> str:
    """Render a value the way migration/baseline/*.csv does (SQL Server sqlcmd/bcp style)."""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value:.4f}"
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S.") + f"{value.microsecond // 1000:03d}"
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    return str(value)


def table_rows(con, sql: str) -> list[list[str]]:
    return [[canonical(v) for v in row] for row in con.execute(sql).fetchall()]
