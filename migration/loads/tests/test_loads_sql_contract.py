"""Static checks of the Unit 3 SQL against PLAN.md §2-§4 (no Snowflake needed)."""

from __future__ import annotations

import re

import pytest

import loads_duckdb_mirror as mirror

SQL_FILES = sorted(mirror.LOADS_SQL.glob("*.sql"))
LOAD_PROCS = {
    "31_sp_load_dimbranch.sql": "OPS.SP_LOAD_DIMBRANCH",
    "32_sp_load_dimaccount.sql": "OPS.SP_LOAD_DIMACCOUNT",
    "33_sp_load_dimcustomer.sql": "OPS.SP_LOAD_DIMCUSTOMER",
    "34_sp_load_facttransaction.sql": "OPS.SP_LOAD_FACTTRANSACTION",
    "35_sp_load_facttransaction_incremental.sql": "OPS.SP_LOAD_FACTTRANSACTION_INCREMENTAL",
}
ALL_PROC_FILES = {**LOAD_PROCS, "20_sp_copy_raw.sql": "OPS.SP_COPY_RAW"}
RAW_TABLES = {
    "STATE", "CITY", "CUSTOMER", "ACCOUNT", "BRANCH", "TRANSACTION_DB", "TRANSACTION_EXCEL", "TRANSACTION_CSV",
}


def code(path):
    return mirror.strip_comments(path.read_text(encoding="utf-8"))


def all_code():
    return "\n".join(code(p) for p in SQL_FILES)


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.name)
def test_header_and_database(path):
    text = path.read_text(encoding="utf-8")
    assert text.startswith("--")
    assert re.search(r"^USE DATABASE BANKING_DWH;$", code(path), re.M)


def test_unit1_objects_are_referenced_not_created():
    created = re.findall(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TRANSIENT\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.]+)", all_code()
    )
    assert not [t for t in created if t.upper().startswith("DWH.")]
    assert "OPS.ETL_ERROR_LOG" not in created and "OPS.FACTTRANSACTION_REJECTS" not in created
    assert {t.split(".")[1] for t in created if t.startswith("RAW.")} == RAW_TABLES


@pytest.mark.parametrize("table", sorted(RAW_TABLES))
def test_raw_tables_carry_metadata_columns(table):
    ddl = code(mirror.LOADS_SQL / "10_raw_tables.sql")
    block = re.search(rf"RAW\.{table} \((.*?)\n\)", ddl, re.S).group(1)
    assert "_SOURCE_FILE VARCHAR" in block
    assert "_SOURCE_ROW NUMBER" in block
    assert "_LOADED_AT TIMESTAMP_NTZ DEFAULT SYSDATE()" in block


@pytest.mark.parametrize("filename", sorted(LOAD_PROCS))
def test_load_procedure_signature(filename):
    name = LOAD_PROCS[filename]
    assert re.search(
        rf"CREATE OR REPLACE PROCEDURE {re.escape(name)}\(\)\s+RETURNS VARCHAR\s+LANGUAGE SQL\b",
        code(mirror.LOADS_SQL / filename),
    )


@pytest.mark.parametrize("filename", sorted(ALL_PROC_FILES))
def test_error_handling_logs_then_reraises(filename):
    body = code(mirror.LOADS_SQL / filename)
    handler = re.search(r"\nEXCEPTION\s+WHEN OTHER THEN(.*?)\n\s*RAISE;\s*\nEND;", body, re.S)
    assert handler, "EXCEPTION WHEN OTHER ... RAISE; END; expected (PLAN.md §2)"
    block = handler.group(1)
    assert "INSERT INTO OPS.ETL_ERROR_LOG (PROCEDURE_NAME, SQLCODE, SQLSTATE, SQLERRM, CONTEXT)" in block
    assert f"'{ALL_PROC_FILES[filename]}'" in block


@pytest.mark.parametrize(
    "filename", ["31_sp_load_dimbranch.sql", "32_sp_load_dimaccount.sql", "33_sp_load_dimcustomer.sql"]
)
def test_dimensions_use_insert_only_merge(filename):
    body = code(mirror.LOADS_SQL / filename)
    assert re.search(r"MERGE INTO DWH\.\w+ AS TGT", body)
    assert "WHEN NOT MATCHED THEN INSERT" in body
    assert "WHEN MATCHED" not in body
    assert "SCD1" in (mirror.LOADS_SQL / filename).read_text(encoding="utf-8")


def test_streams_cover_the_three_fact_sources():
    streams = re.findall(r"CREATE (?:OR REPLACE )?STREAM (?:IF NOT EXISTS )?([\w.]+)\s+ON TABLE ([\w.]+)", all_code())
    assert {t for _, t in streams} == {"RAW.TRANSACTION_DB", "RAW.TRANSACTION_EXCEL", "RAW.TRANSACTION_CSV"}
    assert set(dict(streams)) == set(mirror.STREAM_SOURCES)


def test_task_graph_mirrors_talend_run_order():
    tasks = code(mirror.LOADS_SQL / "50_tasks.sql")
    edges, calls = {}, {}
    for block in tasks.split("CREATE OR REPLACE TASK ")[1:]:
        name = block.split()[0]
        calls[name] = re.search(r"CALL ([\w.]+)\(", block).group(1)
        after = re.search(r"\bAFTER ([\w.]+)", block)
        if after:
            edges[name] = after.group(1)
    chain, node = [], "OPS.TASK_LOAD_FACTTRANSACTION"
    while node:
        chain.append(calls[node])
        node = edges.get(node)
    assert chain[::-1] == [
        "OPS.SP_COPY_RAW",
        "OPS.SP_LOAD_DIMBRANCH",
        "OPS.SP_LOAD_DIMACCOUNT",
        "OPS.SP_LOAD_DIMCUSTOMER",
        "OPS.SP_LOAD_FACTTRANSACTION_INCREMENTAL",
    ]
    assert "SCHEDULE" in tasks.split("CREATE OR REPLACE TASK OPS.TASK_LOAD_DIMBRANCH")[0]


def test_snowpipes_match_the_batch_copy():
    pipes = code(mirror.LOADS_SQL / "60_snowpipes.sql")
    assert not re.search(r"AUTO_INGEST\s*=\s*TRUE", pipes), "auto-ingest is environment-specific; keep it commented"
    copies = mirror.copy_statements()
    for target in ("RAW.TRANSACTION_CSV", "RAW.TRANSACTION_EXCEL"):
        spec = copies[target]
        match = re.search(
            rf"COPY INTO {target} \(.*?FROM @OPS\.LANDING_STAGE/([\w/]+?)/\s*\)"
            r"\s*FILE_FORMAT = \(FORMAT_NAME = '([\w.]+)'\)",
            pipes,
            re.S,
        )
        assert match, target
        assert match.group(1) == spec["stage_path"]
        assert match.group(2) == spec["file_format"]
        pipe_formats = re.findall(r"TO_TIMESTAMP_NTZ\(\$3, '([^']+)'\)", match.group(0))
        copy_formats = [re.search(r"'([^']+)'", e).group(1) for e in spec["exprs"] if "TO_TIMESTAMP_NTZ" in e]
        assert pipe_formats == copy_formats


def test_copy_formats_match_the_sources():
    copies = mirror.copy_statements()
    assert set(copies) == {f"RAW.{t}" for t in RAW_TABLES}
    assert copies["RAW.TRANSACTION_CSV"]["file_format"] == "OPS.FF_CSV_TRANSACTION"
    assert "TO_TIMESTAMP_NTZ($3, 'DD-MM-YYYY HH24:MI:SS')" in copies["RAW.TRANSACTION_CSV"]["exprs"]
    for target, spec in copies.items():
        assert spec["columns"][-2:] == ["_SOURCE_FILE", "_SOURCE_ROW"], target
        if target != "RAW.TRANSACTION_CSV":
            assert spec["file_format"] == "OPS.FF_CSV_EXTRACT", target
