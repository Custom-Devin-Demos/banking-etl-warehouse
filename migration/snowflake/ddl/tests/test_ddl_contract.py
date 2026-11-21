"""Offline contract checks for the Snowflake DDL (migration/snowflake/ddl).

Parses every deploy script and asserts that the tables, columns, types and constraints match
migration/PLAN.md: section 1.2 (read from PLAN.md and mapped with the section 2 type rules) and section 3
(the lineage columns and the OPS tables). No Snowflake account or connector is needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

DDL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = DDL_DIR.parents[2]
PLAN = REPO_ROOT / "migration" / "PLAN.md"
README = DDL_DIR / "README.md"

# PLAN.md section 2: T-SQL type -> Snowflake type, as written in the DDL.
TSQL_TO_SNOWFLAKE = {
    "INT": "INT",
    "MONEY": "NUMBER(19,4)",
    "DATETIME": "TIMESTAMP_NTZ(3)",
    "DATE": "DATE",
}

# PLAN.md section 3: FactTransaction lineage columns.
LINEAGE_COLUMNS = {"SOURCESYSTEM": "VARCHAR(10)", "LOADEDAT": "TIMESTAMP_NTZ(3)"}

# PLAN.md section 3: OPS tables.
OPS_TABLES = {
    "OPS.ETL_ERROR_LOG": {
        "LOG_ID": "INT",
        "LOGGED_AT": "TIMESTAMP_NTZ",
        "PROCEDURE_NAME": "VARCHAR",
        "SQLCODE": "NUMBER",
        "SQLSTATE": "VARCHAR(5)",
        "SQLERRM": "VARCHAR",
        "CONTEXT": "VARIANT",
    },
    "OPS.FACTTRANSACTION_REJECTS": {
        "TRANSACTION_ID": "INT",
        "ACCOUNT_ID": "INT",
        "TRANSACTION_DATE": "TIMESTAMP_NTZ(3)",
        "AMOUNT": "NUMBER(19,4)",
        "TRANSACTION_TYPE": "VARCHAR(50)",
        "BRANCH_ID": "INT",
        "SOURCESYSTEM": "VARCHAR(10)",
        "REJECTREASON": "VARCHAR",
        "REJECTEDAT": "TIMESTAMP_NTZ",
    },
}

SCHEMAS = {"RAW", "DWH", "OPS"}
DATABASE = "BANKING_DWH"


@dataclass
class Table:
    name: str
    file: str
    columns: dict[str, str] = field(default_factory=dict)
    column_defs: dict[str, str] = field(default_factory=dict)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: dict[str, tuple[str, str]] = field(default_factory=dict)
    constraint_texts: list[str] = field(default_factory=list)
    cluster_by: str | None = None


def strip_comments(sql: str) -> str:
    """Remove -- and /* */ comments outside string literals."""
    out, i, n = [], 0, len(sql)
    while i < n:
        if sql[i] == "'":
            j = i + 1
            while j < n and not (sql[j] == "'" and (j + 1 >= n or sql[j + 1] != "'")):
                j += 2 if sql[j] == "'" else 1
            out.append(sql[i : j + 1])
            i = j + 1
        elif sql.startswith("--", i):
            while i < n and sql[i] != "\n":
                i += 1
        elif sql.startswith("/*", i):
            i = sql.index("*/", i) + 2
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


def split_top_level(text: str, sep: str) -> list[str]:
    parts, depth, quote, buf = [], 0, False, []
    for ch in text:
        if ch == "'":
            quote = not quote
        elif not quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == sep and depth == 0:
                parts.append("".join(buf).strip())
                buf = []
                continue
        buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf).strip())
    return parts


def matching_paren(text: str, start: int) -> int:
    depth, quote = 0, False
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "'":
            quote = not quote
        elif not quote and ch == "(":
            depth += 1
        elif not quote and ch == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unbalanced parentheses")


def statements(path: Path) -> list[str]:
    return [s for s in split_top_level(strip_comments(path.read_text()), ";") if s]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().upper()


TYPE_RE = re.compile(r"^([A-Z_]+(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?)")
CREATE_TABLE_RE = re.compile(r"^CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Z_]+\.[A-Z_]+)\s*\(", re.I)


def parse_table(stmt: str, file: str) -> Table:
    m = CREATE_TABLE_RE.match(stmt)
    assert m, f"{file}: tables must be created with CREATE TABLE IF NOT EXISTS schema.table: {stmt[:80]}"
    table = Table(name=m.group(1).upper(), file=file)
    open_idx = m.end() - 1
    close_idx = matching_paren(stmt, open_idx)
    for item in split_top_level(stmt[open_idx + 1 : close_idx], ","):
        item_n = norm(item)
        if item_n.startswith("CONSTRAINT"):
            table.constraint_texts.append(item_n)
            pk = re.search(r"PRIMARY KEY \(([^)]*)\)", item_n)
            fk = re.search(r"FOREIGN KEY \(([^)]*)\) REFERENCES ([A-Z_.]+) \(([^)]*)\)", item_n)
            if pk:
                table.primary_key = [c.strip() for c in pk.group(1).split(",")]
            if fk:
                table.foreign_keys[fk.group(1).strip()] = (fk.group(2), fk.group(3).strip())
            continue
        name, rest = item_n.split(" ", 1)
        tm = TYPE_RE.match(rest)
        assert tm, f"{file}: cannot parse type of {name}"
        table.columns[name] = re.sub(r"\s+", "", tm.group(1))
        table.column_defs[name] = rest
    tail = norm(stmt[close_idx + 1 :])
    cm = re.search(r"CLUSTER BY \(", tail)
    if cm:
        table.cluster_by = tail[cm.end() : matching_paren(tail, cm.end() - 1)]
    return table


def ddl_files() -> list[Path]:
    return sorted(DDL_DIR.glob("[0-9][0-9]_*.sql"))


def parse_all() -> dict[str, Table]:
    tables: dict[str, Table] = {}
    for path in ddl_files():
        for stmt in statements(path):
            if re.match(r"^CREATE\s+TABLE", stmt, re.I):
                t = parse_table(stmt, path.name)
                assert t.name not in tables, f"{t.name} created twice"
                tables[t.name] = t
    return tables


def plan_dwh_tables() -> dict[str, dict[str, str]]:
    """Read PLAN.md section 1.2 and apply the section 2 type mapping."""
    text = PLAN.read_text()
    section = text.split("### 1.2", 1)[1].split("###", 1)[0]
    expected: dict[str, dict[str, str]] = {}
    for line in section.splitlines():
        m = re.match(r"^\| `(\w+)` \| (.+?) \| (.+?) \|$", line)
        if not m:
            continue
        cols = {}
        for name, tsql_type in re.findall(r"`(\w+) ([A-Z]+(?:\(\d+(?:,\d+)?\))?)`", m.group(2)):
            base = re.match(r"[A-Z]+", tsql_type).group(0)
            cols[name.upper()] = tsql_type if base == "VARCHAR" else TSQL_TO_SNOWFLAKE[tsql_type]
        expected[f"DWH.{m.group(1).upper()}"] = cols
    return expected


def plan_dwh_keys() -> dict[str, tuple[list[str], dict[str, str]]]:
    text = PLAN.read_text()
    section = text.split("### 1.2", 1)[1].split("###", 1)[0]
    keys = {}
    for line in section.splitlines():
        m = re.match(r"^\| `(\w+)` \| .+? \| (.+?) \|$", line)
        if not m:
            continue
        pk = [c.upper() for c in re.findall(r"PK `(\w+)`", m.group(2))]
        fks = {c.upper(): f"DWH.{t.upper()}" for c, t in re.findall(r"FK `(\w+)→(\w+)`", m.group(2))}
        keys[f"DWH.{m.group(1).upper()}"] = (pk, fks)
    return keys


TABLES = parse_all()
EXPECTED_DWH = plan_dwh_tables()


def test_plan_section_1_2_was_read():
    assert set(EXPECTED_DWH) == {"DWH.DIMACCOUNT", "DWH.DIMBRANCH", "DWH.DIMCUSTOMER", "DWH.FACTTRANSACTION"}
    assert sum(len(c) for c in EXPECTED_DWH.values()) == 23


def test_only_contract_tables_are_created():
    assert set(TABLES) == set(EXPECTED_DWH) | set(OPS_TABLES)


@pytest.mark.parametrize("table", sorted(EXPECTED_DWH))
def test_dwh_columns_and_types_match_plan(table):
    expected = dict(EXPECTED_DWH[table])
    if table == "DWH.FACTTRANSACTION":
        expected.update(LINEAGE_COLUMNS)
    assert TABLES[table].columns == expected
    assert list(TABLES[table].columns) == list(expected), "column order must follow PLAN.md section 1.2"


@pytest.mark.parametrize("table", sorted(OPS_TABLES))
def test_ops_columns_and_types_match_plan(table):
    assert TABLES[table].columns == OPS_TABLES[table]


def test_etl_error_log_identity_pattern():
    defs = TABLES["OPS.ETL_ERROR_LOG"].column_defs
    assert "AUTOINCREMENT START 1 INCREMENT 1 ORDER" in defs["LOG_ID"]
    assert "DEFAULT SYSDATE()" in defs["LOGGED_AT"]


@pytest.mark.parametrize("table", sorted(EXPECTED_DWH))
def test_dwh_keys_match_plan_and_are_rely_not_enforced(table):
    pk, fks = plan_dwh_keys()[table]
    t = TABLES[table]
    assert t.primary_key == pk
    assert {c: ref[0] for c, ref in t.foreign_keys.items()} == fks
    for col, (ref_table, ref_col) in t.foreign_keys.items():
        assert TABLES[ref_table].primary_key == [ref_col], f"{col} must reference the PK of {ref_table}"
    for c in t.constraint_texts:
        assert c.endswith("NOT ENFORCED RELY"), c
    for col in pk:
        assert "NOT NULL" in t.column_defs[col], f"{table}.{col}: PK column must be NOT NULL (enforced)"


def test_clustering_only_on_fact():
    assert TABLES["DWH.FACTTRANSACTION"].cluster_by == "TO_DATE(TRANSACTIONDATE)"
    assert [n for n, t in TABLES.items() if t.cluster_by] == ["DWH.FACTTRANSACTION"]
    alters = [norm(s) for p in ddl_files() for s in statements(p) if norm(s).startswith("ALTER TABLE")]
    assert "ALTER TABLE DWH.FACTTRANSACTION CLUSTER BY (TO_DATE(TRANSACTIONDATE))" in alters


def test_schemas_created_idempotently():
    stmts = [norm(s) for s in statements(DDL_DIR / "00_database_schemas.sql")]
    assert stmts[0].startswith(f"CREATE DATABASE IF NOT EXISTS {DATABASE}")
    created = {re.match(r"CREATE SCHEMA IF NOT EXISTS (\w+)", s).group(1) for s in stmts if "SCHEMA" in s}
    assert created == SCHEMAS


@pytest.mark.parametrize("path", ddl_files(), ids=lambda p: p.name)
def test_scripts_are_idempotent_and_database_agnostic(path):
    stmts = [norm(s) for s in statements(path)]
    use = [s for s in stmts if s.startswith("USE ")]
    assert use == [f"USE DATABASE {DATABASE}"], "exactly one USE DATABASE, no USE SCHEMA"
    for s in stmts:
        assert not re.match(r"CREATE OR REPLACE (TRANSIENT |TEMPORARY )?(DATABASE|SCHEMA|TABLE)", s), s
        assert not re.match(r"(DROP|TRUNCATE|DELETE|INSERT|MERGE|UPDATE)\b", s), s
        m = re.match(r"(?:CREATE \w+(?: IF NOT EXISTS)?|ALTER TABLE(?: IF EXISTS)?) ([\w.]+)", s)
        if m and not s.startswith(("CREATE DATABASE", "CREATE SCHEMA")):
            assert m.group(1).count(".") == 1, f"object must be schema-qualified, not db-qualified: {m.group(1)}"


def test_deploy_order_respects_dependencies():
    files = [p.name for p in ddl_files()]
    assert files[0] == "00_database_schemas.sql"
    position = {t.name: files.index(t.file) for t in TABLES.values()}
    for t in TABLES.values():
        for ref_table, _ in t.foreign_keys.values():
            assert position[ref_table] < position[t.name], f"{ref_table} must be created before {t.name}"


def test_readme_deploy_order_matches_files():
    listed = re.findall(r"^\d+\. `(\d\d_[\w]+\.sql)`", README.read_text(), re.M)
    assert listed == [p.name for p in ddl_files()]


def test_sqlfluff_parses_every_script():
    sqlfluff = pytest.importorskip("sqlfluff")
    from sqlfluff.core import FluffConfig, Linter

    linter = Linter(config=FluffConfig.from_path(str(REPO_ROOT)))
    for path in ddl_files():
        parsed = linter.parse_string(path.read_text(), fname=str(path))
        assert not parsed.violations, f"{path.name}: {parsed.violations}"
        assert parsed.tree is not None
        unparsable = [s for s in parsed.tree.recursive_crawl("unparsable")]
        assert not unparsable, f"{path.name}: unparsable segments {unparsable}"
    assert sqlfluff.__version__
