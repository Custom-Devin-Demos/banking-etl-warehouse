"""The committed SQL is what live mode runs: keep it generated, lintable and semantically equal to Python."""

import re

import pytest

from _validation_helpers import validate as v


def test_committed_sql_is_in_sync_with_generator():
    assert v.stale_sql() == []


def test_every_check_has_metrics_and_rowhash_sql():
    files = {p.relative_to(v.SQL_DIR).as_posix() for p in v.SQL_DIR.rglob("*.sql")}
    expected = {p.relative_to(v.SQL_DIR).as_posix() for s in v.ALL_SPECS for p in v.sql_paths(s)}
    assert files == expected


@pytest.mark.parametrize("path", sorted(v.SQL_DIR.rglob("*.sql")), ids=lambda p: p.name)
def test_sql_has_no_templating_and_uses_contract_names(path):
    text = path.read_text()
    assert not re.search(r"&\w|\{\{|\{%|<%", text)
    assert "SNOWFLAKE_" not in text and "banking_dwh" not in text.lower()  # database comes from the connection


def test_contract_object_names():
    names = {s.name for s in v.ALL_SPECS}
    for obj in ("RAW.STATE", "RAW.CITY", "RAW.CUSTOMER", "RAW.ACCOUNT", "RAW.BRANCH", "RAW.TRANSACTION_DB",
                "RAW.TRANSACTION_EXCEL", "RAW.TRANSACTION_CSV", "DWH.DIMACCOUNT", "DWH.DIMBRANCH", "DWH.DIMCUSTOMER",
                "DWH.FACTTRANSACTION", "OPS.FACTTRANSACTION_REJECTS"):
        assert obj in names
    fact = next(s for s in v.DWH_SPECS if s.name == "DWH.FACTTRANSACTION")
    assert not {"SOURCESYSTEM", "LOADEDAT"} & {c.name for c in fact.cols}  # lineage columns excluded


def test_sqlfluff_lint_clean():
    sqlfluff = pytest.importorskip("sqlfluff")
    from sqlfluff.core import FluffConfig, Linter

    cfg = FluffConfig.from_path(str(v.REPO_ROOT))
    result = Linter(config=cfg).lint_paths((str(v.SQL_DIR),))
    violations = [f"{r['filepath']}:{x['start_line_no']} {x['code']} {x['description']}"
                  for r in result.as_records() for x in r["violations"]]
    assert sqlfluff and violations == []


# ---- Emulation: run the generated Snowflake SQL on DuckDB (via sqlglot) and compare with Python -----------
DUCK_TYPES = {"int": "INTEGER", "dec4": "DECIMAL(19, 4)", "date": "DATE", "ts": "TIMESTAMP", "str": "VARCHAR"}
TABLE_SPECS = v.RAW_SPECS + v.DWH_SPECS + v.OPS_SPECS


def _duck(spec, rows):
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    schema, table = spec.name.lower().split(".")
    con.execute(f"CREATE SCHEMA {schema}")
    cols = ", ".join(f"{c.name.lower()} {DUCK_TYPES[c.kind]}" for c in spec.cols)
    con.execute(f"CREATE TABLE {schema}.{table} ({cols})")
    canon = [[v.CANON[k](x) for x, k in zip(r, spec.kinds)] for r in rows]
    if canon:
        con.executemany(f"INSERT INTO {schema}.{table} VALUES ({', '.join('?' * len(spec.cols))})", canon)
    return con


def _run(con, sql):
    sqlglot = pytest.importorskip("sqlglot")
    stmt = sqlglot.transpile(sql, read="snowflake", write="duckdb")[-1]
    cur = con.execute(stmt)
    return [d[0] for d in cur.description], cur.fetchall()


@pytest.mark.parametrize("spec", TABLE_SPECS, ids=lambda s: s.name)
def test_generated_sql_matches_python_on_duckdb(spec):
    rows = v.expected_live(v.Sources())[spec.name]
    con = _duck(spec, rows)
    if spec.distinct:  # re-appended rejects must not double count
        schema_table = spec.name.lower()
        con.execute(f"INSERT INTO {schema_table} SELECT * FROM {schema_table}")
    cols, data = _run(con, v.render_metrics_sql(spec))
    assert v.normalise_actual(dict(zip(cols, data[0]))) == v.compute_metrics(spec, rows)
    _, hashes = _run(con, v.render_rowhashes_sql(spec))
    assert sorted(hashes) == sorted(v.row_hashes(spec, rows))


def test_generated_sql_detects_a_changed_row_on_duckdb():
    spec = next(s for s in v.DWH_SPECS if s.name == "DWH.DIMCUSTOMER")
    rows = v.expected_live(v.Sources())[spec.name]
    con = _duck(spec, rows)
    con.execute("UPDATE dwh.dimcustomer SET customername = 'Shelly Juwita' WHERE customerid = 1")
    cols, data = _run(con, v.render_metrics_sql(spec))
    actual = v.normalise_actual(dict(zip(cols, data[0])))
    expected = v.compute_metrics(spec, rows)
    assert actual["ROW_COUNT"] == expected["ROW_COUNT"] and actual["CHECKSUM"] != expected["CHECKSUM"]
    _, hashes = _run(con, v.render_rowhashes_sql(spec))
    assert v.diff_rows(v.row_hashes(spec, rows), hashes)["changed"] == ["1"]
