"""Offline parity: run each procedure's core SELECT (transpiled Snowflake -> DuckDB with sqlglot) on the
SQL Server baseline DWH tables and check it reproduces migration/baseline/sp_*.csv row for row, in order."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal

import pytest

duckdb = pytest.importorskip("duckdb")
sqlglot = pytest.importorskip("sqlglot")

import sp_port_helpers as h  # noqa: E402

DAILY = "DWH.SP_DAILYTRANSACTION"
BALANCE = "DWH.SP_BALANCEPERCUSTOMER"


def _load_dwh(con, reverse_rows: bool = False) -> None:
    con.execute("CREATE SCHEMA DWH")
    for table, columns in h.DWH_TABLES.items():
        cols = ", ".join(f"{c} {t}" for c, t in columns.items())
        con.execute(f"CREATE TABLE DWH.{table} ({cols})")
        spec = "{" + ", ".join(f"'{c}': '{t}'" for c, t in columns.items()) + "}"
        order = "ORDER BY 1 DESC" if reverse_rows else ""
        path = (h.BASELINE_DIR / f"{table}.csv").as_posix()
        con.execute(f"INSERT INTO DWH.{table} SELECT * FROM read_csv('{path}', header = true, columns = {spec}) {order}")


@pytest.fixture(scope="module")
def con():
    connection = duckdb.connect()
    _load_dwh(connection)
    yield connection
    connection.close()


def run_core(con, name: str, sql_override: str | None = None, **params):
    sql = h.bind(sql_override or h.core_select(h.read_sql(name)), {k.upper(): v for k, v in params.items()})
    duck_sql = sqlglot.transpile(sql, read="snowflake", write="duckdb")[0]
    cur = con.execute(duck_sql)
    header = [d[0] for d in cur.description]
    return header, [[h.canonical(v) for v in row] for row in cur.fetchall()]


def read_baseline(path):
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    return rows[0], rows[1:]


def _baseline_cases():
    for path in sorted(h.BASELINE_DIR.glob("sp_DailyTransaction__*.csv")):
        _, start, end = path.stem.split("__")
        yield pytest.param(DAILY, {"start_date": date.fromisoformat(start), "end_date": date.fromisoformat(end)},
                           path, id=path.stem)
    for path in sorted(h.BASELINE_DIR.glob("sp_BalancePerCustomer__*.csv")):
        _, name = path.stem.split("__")
        # "__all" is the empty-string call: LIKE '%' + '' + '%' matches every customer.
        yield pytest.param(BALANCE, {"customer_name": "" if name == "all" else name}, path, id=path.stem)


BASELINE_CASES = list(_baseline_cases())


def test_every_baseline_file_is_covered():
    assert len(BASELINE_CASES) == len(list(h.BASELINE_DIR.glob("sp_*.csv"))) == 4


@pytest.mark.parametrize("name,params,path", BASELINE_CASES)
def test_core_select_reproduces_sql_server_baseline(con, name, params, path):
    header, rows = run_core(con, name, **params)
    base_header, base_rows = read_baseline(path)
    assert [c.upper() for c in header] == [c.upper() for c in base_header]
    assert rows == base_rows


@pytest.mark.parametrize("name,params,path", BASELINE_CASES)
def test_result_order_does_not_depend_on_storage_order(name, params, path):
    shuffled = duckdb.connect()
    _load_dwh(shuffled, reverse_rows=True)
    _, rows = run_core(shuffled, name, **params)
    assert rows == read_baseline(path)[1]


@pytest.mark.parametrize("needle", ["shelly", "SHELLY", "Shelly", "sHeLlY JuWiTa", "juwita", "LLY JU"])
def test_customer_name_match_is_case_insensitive_like_sql_server_ci_collation(con, needle):
    _, rows = run_core(con, BALANCE, customer_name=needle)
    assert rows == read_baseline(h.BASELINE_DIR / "sp_BalancePerCustomer__shelly.csv")[1]


def test_plain_like_would_lose_the_case_insensitive_match(con):
    """Why ILIKE: Snowflake LIKE is case-sensitive (as is DuckDB's), so a literal port returns nothing."""
    sql = h.core_select(h.read_sql(BALANCE))
    assert " ILIKE " in sql
    _, rows = run_core(con, BALANCE, sql_override=sql.replace(" ILIKE ", " LIKE "), customer_name="shelly")
    assert rows == []


def test_unknown_customer_returns_empty_result(con):
    assert run_core(con, BALANCE, customer_name="no such customer")[1] == []


def test_date_range_is_inclusive_on_both_ends_despite_time_of_day(con):
    # 2024-01-20 rows have non-midnight times; BETWEEN on CAST(... AS DATE) must still include them.
    _, rows = run_core(con, DAILY, start_date=date(2024, 1, 20), end_date=date(2024, 1, 20))
    assert rows == [["2024-01-20", "3", "2000000.0000"]]
    _, rows = run_core(con, DAILY, start_date=date(2024, 1, 21), end_date=date(2024, 1, 22))
    assert [r[0] for r in rows] == ["2024-01-21", "2024-01-22"]


def test_range_with_no_transactions_returns_empty_result(con):
    assert run_core(con, DAILY, start_date=date(2023, 1, 1), end_date=date(2023, 12, 31))[1] == []


def test_balances_match_an_independent_python_oracle(con):
    """CURRENTBALANCE = Balance + sum(+Deposit, -everything else), recomputed from the CSVs in plain Python."""
    with (h.BASELINE_DIR / "FactTransaction.csv").open(newline="", encoding="utf-8") as fh:
        delta = defaultdict(Decimal)
        for row in csv.DictReader(fh):
            sign = 1 if row["TransactionType"].upper() == "DEPOSIT" else -1
            delta[row["AccountID"]] += sign * Decimal(row["Amount"])
    with (h.BASELINE_DIR / "DimCustomer.csv").open(newline="", encoding="utf-8") as fh:
        names = {r["CustomerID"]: r["CustomerName"] for r in csv.DictReader(fh)}
    with (h.BASELINE_DIR / "DimAccount.csv").open(newline="", encoding="utf-8") as fh:
        expected = [
            [names[a["CustomerID"]], a["AccountType"], f"{Decimal(a['Balance']):.4f}",
             f"{Decimal(a['Balance']) + delta[a['AccountID']]:.4f}"]
            for a in sorted(csv.DictReader(fh), key=lambda r: int(r["AccountID"]))
            if a["Status"].upper() == "ACTIVE" and a["CustomerID"] in names
        ]
    assert run_core(con, BALANCE, customer_name="")[1] == expected


def test_daily_totals_match_an_independent_python_oracle(con):
    with (h.BASELINE_DIR / "FactTransaction.csv").open(newline="", encoding="utf-8") as fh:
        per_day = defaultdict(lambda: [0, Decimal(0)])
        for row in csv.DictReader(fh):
            day = row["TransactionDate"][:10]
            per_day[day][0] += 1
            per_day[day][1] += Decimal(row["Amount"])
    expected = [[d, str(n), f"{s:.4f}"] for d, (n, s) in sorted(per_day.items())]
    _, rows = run_core(con, DAILY, start_date=date(1900, 1, 1), end_date=date(9999, 12, 31))
    assert rows == expected


def test_like_wildcards_in_the_parameter_behave_as_in_sql_server(con):
    # '%' and '_' are wildcards in both T-SQL LIKE and Snowflake ILIKE.
    assert run_core(con, BALANCE, customer_name="SH_LLY")[1] == run_core(con, BALANCE, customer_name="shelly")[1]
    assert len(run_core(con, BALANCE, customer_name="%")[1]) == 18


def test_core_select_only_binds_declared_parameters():
    assert h.binds_in(h.core_select(h.read_sql(DAILY))) == {"START_DATE", "END_DATE"}
    assert h.binds_in(h.core_select(h.read_sql(BALANCE))) == {"CUSTOMER_NAME"}


def test_baseline_data_has_no_case_or_padding_variants():
    """The port compares Status/TransactionType case-insensitively (UPPER) to keep CI-collation semantics.
    Trailing-space-insensitive '=' is not reproduced; this asserts the data does not depend on it."""
    for table, cols in (("DimAccount", ["Status"]), ("FactTransaction", ["TransactionType"])):
        with (h.BASELINE_DIR / f"{table}.csv").open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                for col in cols:
                    assert row[col] == row[col].strip(), (table, col, row[col])
                    assert re.fullmatch(r"[A-Za-z]+", row[col]), (table, col, row[col])
