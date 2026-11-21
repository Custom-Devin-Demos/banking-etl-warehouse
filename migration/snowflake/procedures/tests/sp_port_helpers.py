"""Shared helpers for the offline tests of the unit 2 Snowflake procedure ports."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROC_DIR = TESTS_DIR.parent
REPO_ROOT = PROC_DIR.parents[2]
BASELINE_DIR = REPO_ROOT / "migration" / "baseline"
PLAN_MD = REPO_ROOT / "migration" / "PLAN.md"

PROCEDURE_FILES = {
    "DWH.SP_DAILYTRANSACTION": PROC_DIR / "sp_dailytransaction.sql",
    "DWH.SP_BALANCEPERCUSTOMER": PROC_DIR / "sp_balancepercustomer.sql",
}

# Column types of the DWH tables per PLAN.md sections 1.2 and 2 (MONEY -> NUMBER(19,4), DATETIME -> TIMESTAMP_NTZ(3)),
# expressed as DuckDB types so the baseline CSVs load with the same precision Snowflake would store.
DWH_TABLES = {
    "DimAccount": {
        "AccountID": "INTEGER",
        "CustomerID": "INTEGER",
        "AccountType": "VARCHAR",
        "Balance": "DECIMAL(19,4)",
        "DateOpened": "DATE",
        "Status": "VARCHAR",
    },
    "DimBranch": {"BranchID": "INTEGER", "BranchName": "VARCHAR", "BranchLocation": "VARCHAR"},
    "DimCustomer": {
        "CustomerID": "INTEGER",
        "CustomerName": "VARCHAR",
        "Address": "VARCHAR",
        "CityName": "VARCHAR",
        "StateName": "VARCHAR",
        "Age": "INTEGER",
        "Gender": "VARCHAR",
        "Email": "VARCHAR",
    },
    "FactTransaction": {
        "TransactionID": "INTEGER",
        "AccountID": "INTEGER",
        "TransactionDate": "TIMESTAMP",
        "Amount": "DECIMAL(19,4)",
        "TransactionType": "VARCHAR",
        "BranchID": "INTEGER",
    },
}

_BIND_RE = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")


def read_sql(name: str) -> str:
    return PROCEDURE_FILES[name].read_text(encoding="utf-8")


def procedure_body(sql: str) -> str:
    match = re.search(r"\$\$\n(.*?)\$\$;", sql, re.S)
    assert match, "procedure body must be a $$-quoted Snowflake Scripting block"
    return match.group(1)


def core_select(sql: str) -> str:
    """The query assigned to the RESULTSET, i.e. the procedure's T-SQL SELECT after porting."""
    match = re.search(r"-- core-select:begin\n\s*res := \((.*)\);\s*\n\s*-- core-select:end", sql, re.S)
    assert match, "core select must sit between '-- core-select:begin' and '-- core-select:end'"
    return match.group(1).strip()


def handler_insert(sql: str) -> str:
    match = re.search(r"(INSERT INTO OPS\.ETL_ERROR_LOG.*?;)", procedure_body(sql), re.S)
    assert match, "EXCEPTION handler must INSERT INTO OPS.ETL_ERROR_LOG"
    return match.group(1)


def sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, date):
        return f"CAST('{value.isoformat()}' AS DATE)"
    return "'" + str(value).replace("'", "''") + "'"


def bind(sql: str, params: dict) -> str:
    """Replace Snowflake Scripting ``:VAR`` binds with SQL literals (unknown binds are an error)."""

    def repl(match: re.Match) -> str:
        key = match.group(1).upper()
        if key not in params:
            raise KeyError(f"no value for bind :{match.group(1)}")
        return sql_literal(params[key])

    return _BIND_RE.sub(repl, sql)


def binds_in(sql: str) -> set[str]:
    return {m.upper() for m in _BIND_RE.findall(sql)}


def canonical(value) -> str:
    """Canonical text form used to compare query results with the SQL Server baseline CSVs."""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value:.4f}"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)
