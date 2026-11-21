"""Structural checks of the Snowflake Scripting procedure scripts against the PLAN.md contract.

Needs only the standard library; the sqlglot / sqlfluff checks skip when those packages are missing."""

from __future__ import annotations

import re
from datetime import date

import pytest

import sp_port_helpers as h

PROCS = sorted(h.PROCEDURE_FILES)
EXPECTED_PARAMS = {"DWH.SP_DAILYTRANSACTION": ["START_DATE", "END_DATE"], "DWH.SP_BALANCEPERCUSTOMER": ["CUSTOMER_NAME"]}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\s*([(),])\s*", r"\1", text)).strip().upper()


def _plan_signatures() -> dict[str, str]:
    plan = h.PLAN_MD.read_text(encoding="utf-8")
    sigs = {}
    for name in PROCS:
        match = re.search(r"`(" + re.escape(name) + r"\(.*?\) RETURNS TABLE \(.*?\))`", plan)
        assert match, f"signature for {name} not found in PLAN.md section 3"
        sigs[name] = match.group(1)
    return sigs


def _signature(sql: str, name: str) -> str:
    match = re.search(r"CREATE OR REPLACE PROCEDURE (" + re.escape(name) + r"\(.*?\)\s*RETURNS TABLE \(.*?\))\n", sql, re.S)
    assert match, f"CREATE OR REPLACE PROCEDURE {name}(...) RETURNS TABLE (...) not found"
    return match.group(1)


def _strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


@pytest.mark.parametrize("name", PROCS)
def test_signature_matches_plan_contract(name):
    assert _norm(_signature(h.read_sql(name), name)) == _norm(_plan_signatures()[name])


@pytest.mark.parametrize("name", PROCS)
def test_deploy_script_shape(name):
    sql = _strip_comments(h.read_sql(name))
    statements = [s.strip() for s in re.split(r";\s*(?=\n(?:USE|CREATE)|\Z)", sql.strip()) if s.strip()]
    assert statements[0] == "USE DATABASE BANKING_DWH"
    assert len(statements) == 2 and statements[1].startswith(f"CREATE OR REPLACE PROCEDURE {name}(")
    header = sql.split("$$")[0]
    assert "\nLANGUAGE SQL\n" in header
    assert "\nEXECUTE AS OWNER\n" in header
    assert sql.rstrip().endswith("$$;")


@pytest.mark.parametrize("name", PROCS)
def test_returns_the_resultset_as_a_table(name):
    body = h.procedure_body(h.read_sql(name))
    assert re.search(r"\n\s*res RESULTSET;", body)
    assert "RETURN TABLE(res);" in body


@pytest.mark.parametrize("name", PROCS)
def test_no_tsql_constructs_left(name):
    body = _strip_comments(h.procedure_body(h.read_sql(name)))
    for pattern in [r"@\w", r"\bISNULL\s*\(", r"\bSET\s+NOCOUNT\b", r"\bPRINT\b", r"^\s*GO\s*$", r"\[\w+\]",
                    r"'\s*\+|\+\s*'", r"\bdbo\.", r"\bGETDATE\b", r"\bTOP\s+\d", r"\bBEGIN\s+TRY\b"]:
        assert not re.search(pattern, body, re.I | re.M), pattern


@pytest.mark.parametrize("name", PROCS)
def test_tables_are_schema_qualified(name):
    sql = h.core_select(h.read_sql(name))
    ctes = set(re.findall(r"\b(\w+) AS \(", sql))
    refs = re.findall(r"\b(?:FROM|JOIN)\s+([\w.]+)", sql)
    assert refs
    for ref in refs:
        assert ref in ctes or re.fullmatch(r"DWH\.\w+", ref), ref


@pytest.mark.parametrize("name", PROCS)
def test_core_select_is_deterministically_ordered(name):
    assert re.search(r"\bORDER BY\b", h.core_select(h.read_sql(name)))


def test_balance_name_filter_is_case_insensitive_and_uses_coalesce():
    sql = h.core_select(h.read_sql("DWH.SP_BALANCEPERCUSTOMER"))
    assert "ILIKE ('%' || :CUSTOMER_NAME || '%')" in sql
    assert "COALESCE(TS.TOTALTRANSACTIONAMOUNT, 0)" in sql
    assert re.search(r"ORDER BY A\.ACCOUNTID\s*$", sql)


def test_daily_filter_stays_inclusive_on_dates():
    sql = h.core_select(h.read_sql("DWH.SP_DAILYTRANSACTION"))
    assert "WHERE CAST(TRANSACTIONDATE AS DATE) BETWEEN :START_DATE AND :END_DATE" in sql


def _declared_exceptions(body: str):
    return re.findall(r"\n\s*(\w+) EXCEPTION \((-\d+), '([^']*)'\);", body)


def test_declared_exception_codes_are_user_range_and_unique():
    codes = []
    for name in PROCS:
        excs = _declared_exceptions(h.procedure_body(h.read_sql(name)))
        assert excs, name
        for exc_name, code, message in excs:
            assert -20999 <= int(code) <= -20000, (exc_name, code)
            assert message.startswith(name.split(".")[1] + ":"), message
            codes.append(int(code))
    assert len(codes) == len(set(codes))


@pytest.mark.parametrize("name", PROCS)
def test_parameters_are_validated_before_the_query(name):
    body = h.procedure_body(h.read_sql(name))
    main = body.split("EXCEPTION\n    WHEN OTHER THEN")[0]
    first_query = main.index("res := (")
    null_check = " OR ".join(f"{p} IS NULL" for p in EXPECTED_PARAMS[name])
    assert main.index(f"IF ({null_check}) THEN\n        RAISE null_parameter;") < first_query
    if name == "DWH.SP_DAILYTRANSACTION":
        assert main.index("IF (START_DATE > END_DATE) THEN\n        RAISE invalid_date_range;") < first_query
    declared = {n for n, _, _ in _declared_exceptions(body)}
    assert set(re.findall(r"RAISE (\w+);", main)) == declared


@pytest.mark.parametrize("name", PROCS)
def test_exception_handler_logs_then_reraises(name):
    body = h.procedure_body(h.read_sql(name))
    parts = body.split("\nEXCEPTION\n    WHEN OTHER THEN\n")
    assert len(parts) == 2, "exactly one top-level EXCEPTION WHEN OTHER handler"
    handler = parts[1]
    steps = ["err_code := SQLCODE;", "err_state := SQLSTATE;", "err_message := SQLERRM;",
             "INSERT INTO OPS.ETL_ERROR_LOG (PROCEDURE_NAME, SQLCODE, SQLSTATE, SQLERRM, CONTEXT)",
             f"'{name}',", "RAISE;"]
    positions = [handler.index(s) for s in steps]
    assert positions == sorted(positions)
    assert re.fullmatch(r"\s*RAISE;\s*END;\s*", handler[positions[-1]:])
    assert set(EXPECTED_PARAMS[name]) <= h.binds_in(h.handler_insert(h.read_sql(name)))


def test_embedded_statements_parse_as_snowflake_sql():
    sqlglot = pytest.importorskip("sqlglot")
    params = {"START_DATE": date(2024, 1, 1), "END_DATE": date(2024, 1, 31), "CUSTOMER_NAME": "x",
              "ERR_CODE": -20001, "ERR_STATE": "P0001", "ERR_MESSAGE": "m"}
    for name in PROCS:
        sql = h.read_sql(name)
        for stmt in (h.core_select(sql), h.handler_insert(sql)):
            sqlglot.parse_one(h.bind(stmt, params), read="snowflake", error_level=sqlglot.ErrorLevel.RAISE)


def test_sqlfluff_lints_procedure_scripts_clean():
    pytest.importorskip("sqlfluff")
    from sqlfluff.core import FluffConfig, Linter

    config = FluffConfig.from_path(str(h.REPO_ROOT))
    paths = [str(p) for p in sorted(h.PROC_DIR.glob("*.sql"))]
    result = Linter(config=config).lint_paths(tuple(paths))
    violations = [(v["filepath"], v["violations"]) for v in result.as_records() if v["violations"]]
    assert not violations, violations


def test_sqlfluff_lints_core_selects_clean():
    """sqlfluff cannot parse a Scripting DECLARE/BEGIN block (the $$ body is a string literal to it), so lint
    the ported SELECTs on their own."""
    pytest.importorskip("sqlfluff")
    import textwrap

    from sqlfluff.core import FluffConfig, Linter

    config = FluffConfig.from_path(str(h.REPO_ROOT))
    params = {"START_DATE": date(2024, 1, 1), "END_DATE": date(2024, 1, 31), "CUSTOMER_NAME": "x"}
    for name in PROCS:
        core = h.core_select(h.read_sql(name))
        first, rest = core.split("\n", 1)
        sql = h.bind(first + "\n" + textwrap.dedent(rest), params) + "\n"
        violations = Linter(config=config).lint_string(sql).get_violations()
        assert not violations, (name, [(v.rule_code(), v.desc()) for v in violations])
