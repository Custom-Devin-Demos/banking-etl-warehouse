"""The committed load SQL, run on DuckDB over the extracts, reproduces migration/baseline/ (PLAN.md §1.5, §1.6)."""

from __future__ import annotations

import datetime as dt

import pytest

import loads_duckdb_mirror as mirror

RAW_TABLES = [
    "RAW.STATE",
    "RAW.CITY",
    "RAW.CUSTOMER",
    "RAW.ACCOUNT",
    "RAW.BRANCH",
    "RAW.TRANSACTION_DB",
    "RAW.TRANSACTION_EXCEL",
    "RAW.TRANSACTION_CSV",
]
T0 = dt.datetime(2026, 1, 1, 1, 0, 0)

DIM_QUERIES = {
    "DimBranch.csv": "SELECT BRANCHID, BRANCHNAME, BRANCHLOCATION FROM DWH.DIMBRANCH ORDER BY 1",
    "DimAccount.csv": (
        "SELECT ACCOUNTID, CUSTOMERID, ACCOUNTTYPE, BALANCE, DATEOPENED, STATUS FROM DWH.DIMACCOUNT ORDER BY 1"
    ),
    "DimCustomer.csv": (
        "SELECT CUSTOMERID, CUSTOMERNAME, ADDRESS, CITYNAME, STATENAME, AGE, GENDER, EMAIL "
        "FROM DWH.DIMCUSTOMER ORDER BY 1"
    ),
}
FACT_QUERY = (
    "SELECT TRANSACTIONID, ACCOUNTID, TRANSACTIONDATE, AMOUNT, TRANSACTIONTYPE, BRANCHID "
    "FROM DWH.FACTTRANSACTION ORDER BY 1"
)
EXPECTED_REJECTS = [
    ["6", "EXCEL", "DUPLICATE_LOWER_PRECEDENCE"],
    ["7", "EXCEL", "DUPLICATE_LOWER_PRECEDENCE"],
    ["14", "CSV", "DUPLICATE_LOWER_PRECEDENCE"],
    ["15", "CSV", "DUPLICATE_LOWER_PRECEDENCE"],
    ["23", "CSV", "ORPHAN_ACCOUNT"],
    ["24", "CSV", "ORPHAN_ACCOUNT"],
    ["25", "CSV", "ORPHAN_ACCOUNT"],
]


def baseline_rows(name):
    return [list(r.values()) for r in mirror.read_baseline(name)]


def land_all(con, excel_csv, loaded_at=T0):
    counts = {}
    for i, table in enumerate(RAW_TABLES):
        source = mirror.source_file_for(table, excel_csv)
        counts[table] = mirror.copy_into(con, table, source, loaded_at + dt.timedelta(seconds=i))
    return counts


@pytest.fixture()
def loaded(con, excel_csv):
    land_all(con, excel_csv)
    mirror.load_dims(con)
    mirror.run_full_fact(con)
    return con


def test_copy_into_lands_every_source_row(con, excel_csv):
    assert land_all(con, excel_csv) == {
        "RAW.STATE": 9,
        "RAW.CITY": 52,
        "RAW.CUSTOMER": 20,
        "RAW.ACCOUNT": 21,
        "RAW.BRANCH": 5,
        "RAW.TRANSACTION_DB": 10,
        "RAW.TRANSACTION_EXCEL": 7,
        "RAW.TRANSACTION_CSV": 12,
    }
    meta = con.execute(
        "SELECT MIN(_SOURCE_ROW), MAX(_SOURCE_ROW), ANY_VALUE(_SOURCE_FILE) FROM RAW.TRANSACTION_CSV"
    ).fetchone()
    assert meta == (1, 12, "files/transaction_csv/transaction_csv.csv.gz")


def test_csv_dates_parsed_day_first(con, excel_csv):
    land_all(con, excel_csv)
    got = con.execute("SELECT TRANSACTION_DATE FROM RAW.TRANSACTION_CSV WHERE TRANSACTION_ID = 14").fetchone()[0]
    assert got == dt.datetime(2024, 1, 21, 14, 0)


@pytest.mark.parametrize("name", sorted(DIM_QUERIES))
def test_dimension_matches_baseline(loaded, name):
    assert mirror.table_rows(loaded, DIM_QUERIES[name]) == baseline_rows(name)


def test_fact_matches_baseline(loaded):
    assert mirror.table_rows(loaded, FACT_QUERY) == baseline_rows("FactTransaction.csv")
    assert len(baseline_rows("FactTransaction.csv")) == 22


def test_fact_precedence_db_over_excel_over_csv(loaded):
    lineage = dict(loaded.execute("SELECT TRANSACTIONID, SOURCESYSTEM FROM DWH.FACTTRANSACTION").fetchall())
    assert lineage[6] == lineage[7] == "DB"
    assert lineage[14] == lineage[15] == "EXCEL"
    assert {lineage[i] for i in range(16, 23)} == {"CSV"}
    assert loaded.execute("SELECT COUNT(*) FROM DWH.FACTTRANSACTION WHERE LOADEDAT IS NULL").fetchone()[0] == 0


def test_rejects_are_written_not_dropped(loaded):
    got = mirror.table_rows(
        loaded, "SELECT TRANSACTION_ID, SOURCESYSTEM, REJECTREASON FROM OPS.FACTTRANSACTION_REJECTS ORDER BY 1, 2"
    )
    assert got == EXPECTED_REJECTS
    excel_6 = loaded.execute(
        "SELECT TRANSACTION_DATE, AMOUNT FROM OPS.FACTTRANSACTION_REJECTS WHERE TRANSACTION_ID = 6"
    ).fetchone()
    assert excel_6[0] == dt.datetime(2024, 1, 18, 13, 10)


def test_every_raw_transaction_is_loaded_or_rejected(loaded):
    raw = loaded.execute(
        "SELECT (SELECT COUNT(*) FROM RAW.TRANSACTION_DB) + (SELECT COUNT(*) FROM RAW.TRANSACTION_EXCEL)"
        " + (SELECT COUNT(*) FROM RAW.TRANSACTION_CSV)"
    ).fetchone()[0]
    fact = loaded.execute("SELECT COUNT(*) FROM DWH.FACTTRANSACTION").fetchone()[0]
    rejects = loaded.execute("SELECT COUNT(*) FROM OPS.FACTTRANSACTION_REJECTS").fetchone()[0]
    assert raw == fact + rejects == 29


def test_rerun_is_idempotent(loaded, excel_csv):
    """Talend re-runs hit PK violations on the dims; insert-only MERGE makes a re-run a no-op instead."""
    mirror.load_dims(loaded)
    mirror.run_full_fact(loaded)
    for name, query in DIM_QUERIES.items():
        assert mirror.table_rows(loaded, query) == baseline_rows(name)
    assert mirror.table_rows(loaded, FACT_QUERY) == baseline_rows("FactTransaction.csv")
    assert loaded.execute("SELECT COUNT(*) FROM OPS.FACTTRANSACTION_REJECTS").fetchone()[0] == 14


def test_relanded_files_do_not_change_the_warehouse(loaded, excel_csv):
    """COPY INTO skips already-loaded files; if they are force-reloaded, RAW holds two copies of every row and the
    later copies lose the dedup (earliest load wins), so DWH is unchanged and the copies are logged as rejects."""
    land_all(loaded, excel_csv, loaded_at=T0 + dt.timedelta(hours=1))
    mirror.load_dims(loaded)
    mirror.run_full_fact(loaded)
    for name, query in DIM_QUERIES.items():
        assert mirror.table_rows(loaded, query) == baseline_rows(name)
    assert mirror.table_rows(loaded, FACT_QUERY) == baseline_rows("FactTransaction.csv")
    assert loaded.execute("SELECT COUNT(*) FROM OPS.FACTTRANSACTION_REJECTS").fetchone()[0] == 7 + 7 + 29


def test_dimensions_are_insert_only_not_scd1(loaded, tmp_path):
    changed = tmp_path / "branch.csv"
    changed.write_text(
        "branch_id,branch_name,branch_location\n1,KC Jakarta Renamed,Somewhere\n9,KC New,Jl. Baru\n", encoding="utf-8"
    )
    mirror.copy_into(loaded, "RAW.BRANCH", changed, T0 + dt.timedelta(hours=2))
    mirror.load_dims(loaded)
    rows = dict(loaded.execute("SELECT BRANCHID, BRANCHNAME FROM DWH.DIMBRANCH").fetchall())
    assert rows[1] == "KC Jakarta"
    assert rows[9] == "KC New"


def test_customer_left_joins_keep_unmatched_city(con, excel_csv, tmp_path):
    land_all(con, excel_csv)
    extra = tmp_path / "customer.csv"
    extra.write_text(
        "customer_id,customer_name,address,city_id,age,gender,email\n99,Ana Lee,Jl. X,999,040,female,a@b.c\n",
        encoding="utf-8",
    )
    mirror.copy_into(con, "RAW.CUSTOMER", extra, T0 + dt.timedelta(hours=1))
    mirror.load_dims(con)
    got = con.execute(
        "SELECT CUSTOMERNAME, ADDRESS, CITYNAME, STATENAME, AGE, GENDER FROM DWH.DIMCUSTOMER WHERE CUSTOMERID = 99"
    ).fetchone()
    assert got == ("ANA LEE", "JL. X", None, None, 40, "FEMALE")


def test_orphan_branch_is_rejected(con, excel_csv, tmp_path):
    land_all(con, excel_csv)
    extra = tmp_path / "transaction_csv.csv"
    extra.write_text(
        "transaction_id,account_id,transaction_date,amount,transaction_type,branch_id\n"
        "90,1,01-03-2024 10:00:00,5,Deposit,77\n",
        encoding="utf-8",
    )
    mirror.copy_into(con, "RAW.TRANSACTION_CSV", extra, T0 + dt.timedelta(hours=1))
    mirror.load_dims(con)
    mirror.run_full_fact(con)
    reason = con.execute("SELECT REJECTREASON FROM OPS.FACTTRANSACTION_REJECTS WHERE TRANSACTION_ID = 90").fetchone()
    assert reason == ("ORPHAN_BRANCH",)
    assert con.execute("SELECT COUNT(*) FROM DWH.FACTTRANSACTION WHERE TRANSACTIONID = 90").fetchone()[0] == 0
