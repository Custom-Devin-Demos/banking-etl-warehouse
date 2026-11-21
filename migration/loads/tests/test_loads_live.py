"""Live checks against a deployed and loaded Snowflake account; skipped unless SNOWFLAKE_ACCOUNT is set.

Read-only: run migration/snowflake/loads/90_run_full_load.sql first. Connection env vars per PLAN.md §3.
"""

from __future__ import annotations

import os

import pytest

import loads_duckdb_mirror as mirror

pytestmark = pytest.mark.skipif(not os.environ.get("SNOWFLAKE_ACCOUNT"), reason="SNOWFLAKE_ACCOUNT not set")


@pytest.fixture(scope="module")
def cursor():
    connector = pytest.importorskip("snowflake.connector")
    params = {
        "account": os.environ["SNOWFLAKE_ACCOUNT"],
        "user": os.environ.get("SNOWFLAKE_USER"),
        "role": os.environ.get("SNOWFLAKE_ROLE"),
        "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE"),
        "database": os.environ.get("SNOWFLAKE_DATABASE", "BANKING_DWH"),
    }
    if os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH"):
        params["private_key_file"] = os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"]
        if os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"):
            params["private_key_file_pwd"] = os.environ["SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"]
    else:
        params["password"] = os.environ.get("SNOWFLAKE_PASSWORD")
    conn = connector.connect(**{k: v for k, v in params.items() if v is not None})
    try:
        yield conn.cursor()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "name",
    [
        "SP_COPY_RAW",
        "SP_LOAD_DIMBRANCH",
        "SP_LOAD_DIMACCOUNT",
        "SP_LOAD_DIMCUSTOMER",
        "SP_LOAD_FACTTRANSACTION",
        "SP_LOAD_FACTTRANSACTION_INCREMENTAL",
    ],
)
def test_procedures_deployed(cursor, name):
    cursor.execute(f"SHOW PROCEDURES LIKE '{name}' IN SCHEMA OPS")
    assert cursor.fetchall()


def test_task_graph_deployed(cursor):
    cursor.execute("SHOW TASKS LIKE 'TASK_LOAD_%' IN SCHEMA OPS")
    assert len(cursor.fetchall()) == 5


@pytest.mark.parametrize(
    ("table", "baseline"),
    [
        ("DWH.DIMBRANCH", "DimBranch.csv"),
        ("DWH.DIMACCOUNT", "DimAccount.csv"),
        ("DWH.DIMCUSTOMER", "DimCustomer.csv"),
        ("DWH.FACTTRANSACTION", "FactTransaction.csv"),
    ],
)
def test_row_counts_match_baseline(cursor, table, baseline):
    cursor.execute(f"SELECT COUNT(*) FROM {table}")
    assert cursor.fetchone()[0] == len(mirror.read_baseline(baseline))
