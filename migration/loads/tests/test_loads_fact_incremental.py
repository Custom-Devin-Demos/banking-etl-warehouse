"""Incremental fact path (streams -> OPS.SP_LOAD_FACTTRANSACTION_INCREMENTAL) converges to the full reload."""

from __future__ import annotations

import datetime as dt
import itertools

import pytest

import loads_duckdb_mirror as mirror
from test_loads_baseline_parity import FACT_QUERY, T0, baseline_rows, land_all

DIM_SOURCES = ["RAW.STATE", "RAW.CITY", "RAW.CUSTOMER", "RAW.ACCOUNT", "RAW.BRANCH"]
FACT_SOURCES = ["RAW.TRANSACTION_DB", "RAW.TRANSACTION_EXCEL", "RAW.TRANSACTION_CSV"]


def land_dims(con, excel_csv):
    for i, table in enumerate(DIM_SOURCES):
        mirror.copy_into(con, table, mirror.source_file_for(table, excel_csv), T0 + dt.timedelta(seconds=i))
    mirror.load_dims(con)


def fact(con):
    return mirror.table_rows(con, FACT_QUERY)


@pytest.mark.parametrize("order", list(itertools.permutations(FACT_SOURCES)), ids=lambda o: ">".join(o))
def test_any_arrival_order_gives_the_baseline(con, excel_csv, order):
    land_dims(con, excel_csv)
    for batch, table in enumerate(order, start=1):
        mirror.copy_into(con, table, mirror.source_file_for(table, excel_csv), T0 + dt.timedelta(minutes=batch))
        mirror.run_incremental_fact(con)
    assert fact(con) == baseline_rows("FactTransaction.csv")
    lineage = dict(con.execute("SELECT TRANSACTIONID, SOURCESYSTEM FROM DWH.FACTTRANSACTION").fetchall())
    assert lineage[6] == "DB" and lineage[14] == "EXCEL"


def test_rejects_logged_once_per_losing_row(con, excel_csv):
    land_dims(con, excel_csv)
    for batch, table in enumerate(["RAW.TRANSACTION_CSV", "RAW.TRANSACTION_EXCEL", "RAW.TRANSACTION_DB"], start=1):
        mirror.copy_into(con, table, mirror.source_file_for(table, excel_csv), T0 + dt.timedelta(minutes=batch))
        mirror.run_incremental_fact(con)
    got = con.execute(
        "SELECT TRANSACTION_ID, SOURCESYSTEM, REJECTREASON FROM OPS.FACTTRANSACTION_REJECTS ORDER BY 1, 2"
    ).fetchall()
    assert got == [
        (6, "EXCEL", "DUPLICATE_LOWER_PRECEDENCE"),
        (7, "EXCEL", "DUPLICATE_LOWER_PRECEDENCE"),
        (14, "CSV", "DUPLICATE_LOWER_PRECEDENCE"),
        (15, "CSV", "DUPLICATE_LOWER_PRECEDENCE"),
        (23, "CSV", "ORPHAN_ACCOUNT"),
        (24, "CSV", "ORPHAN_ACCOUNT"),
        (25, "CSV", "ORPHAN_ACCOUNT"),
    ]


def test_empty_streams_change_nothing(con, excel_csv):
    land_all(con, excel_csv)
    mirror.load_dims(con)
    mirror.run_full_fact(con)
    before = fact(con)
    mirror.run_incremental_fact(con)
    assert fact(con) == before == baseline_rows("FactTransaction.csv")


def test_redelivered_file_after_full_load_is_a_noop_for_the_fact(con, excel_csv):
    land_all(con, excel_csv)
    mirror.load_dims(con)
    mirror.run_full_fact(con)
    mirror.copy_into(
        con, "RAW.TRANSACTION_DB", mirror.source_file_for("RAW.TRANSACTION_DB", excel_csv), T0 + dt.timedelta(hours=1)
    )
    mirror.run_incremental_fact(con)
    assert fact(con) == baseline_rows("FactTransaction.csv")


def test_late_higher_precedence_orphan_removes_fact_row(con, excel_csv, tmp_path):
    """Full-reload semantics: the DB row wins the dedup, then fails the FK check, so the transaction is absent."""
    land_dims(con, excel_csv)
    excel_like = tmp_path / "late_excel.csv"
    excel_like.write_text(
        "transaction_id,account_id,transaction_date,amount,transaction_type,branch_id\n"
        "80,1,2024-03-01 10:00:00,5,Deposit,1\n",
        encoding="utf-8",
    )
    db_like = tmp_path / "late_db.csv"
    db_like.write_text(
        "transaction_id,account_id,transaction_date,amount,transaction_type,branch_id\n"
        "80,999,2024-03-01 10:00:00,5,Deposit,1\n",
        encoding="utf-8",
    )
    mirror.copy_into(con, "RAW.TRANSACTION_EXCEL", excel_like, T0 + dt.timedelta(minutes=1))
    mirror.run_incremental_fact(con)
    assert con.execute("SELECT SOURCESYSTEM FROM DWH.FACTTRANSACTION WHERE TRANSACTIONID = 80").fetchone() == (
        "EXCEL",
    )
    mirror.copy_into(con, "RAW.TRANSACTION_DB", db_like, T0 + dt.timedelta(minutes=2))
    mirror.run_incremental_fact(con)
    assert con.execute("SELECT COUNT(*) FROM DWH.FACTTRANSACTION WHERE TRANSACTIONID = 80").fetchone()[0] == 0
    reasons = con.execute(
        "SELECT SOURCESYSTEM, REJECTREASON FROM OPS.FACTTRANSACTION_REJECTS WHERE TRANSACTION_ID = 80 ORDER BY 1"
    ).fetchall()
    assert reasons == [("DB", "ORPHAN_ACCOUNT"), ("EXCEL", "DUPLICATE_LOWER_PRECEDENCE")]

    full = mirror.new_connection()
    try:
        land_dims(full, excel_csv)
        mirror.copy_into(full, "RAW.TRANSACTION_EXCEL", excel_like, T0 + dt.timedelta(minutes=1))
        mirror.copy_into(full, "RAW.TRANSACTION_DB", db_like, T0 + dt.timedelta(minutes=2))
        mirror.run_full_fact(full)
        assert fact(full) == fact(con)
    finally:
        full.close()
