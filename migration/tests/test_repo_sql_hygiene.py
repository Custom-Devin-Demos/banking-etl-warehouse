"""Hygiene checks for every committed Snowflake SQL file and for credentials in migration/ (offline)."""

from __future__ import annotations

import re

import pytest

from repo_contract import REPO_ROOT, first_code_line, migration_sql_files, scanned_text_files, snowflake_sql_files, strip_sql

SQL_FILES = migration_sql_files()
SNOWFLAKE_FILES = snowflake_sql_files()


def _ids(paths):
    return [p.relative_to(REPO_ROOT).as_posix() for p in paths]


TEMPLATING = [
    (re.compile(r"\{\{|\}\}|\{%|%\}"), "Jinja/dbt templating"),
    (re.compile(r"<%|%>"), "ERB-style templating"),
    (re.compile(r"&\{?[A-Za-z_]\w*\}?"), "SnowSQL &variable substitution"),
]

TSQL_LEFTOVERS = [
    (re.compile(r"^\s*GO\s*;?\s*$", re.I | re.M), "GO batch separator"),
    (re.compile(r"\bSET\s+NOCOUNT\b", re.I), "SET NOCOUNT"),
    (re.compile(r"\bISNULL\s*\(", re.I), "ISNULL() (use COALESCE)"),
    (re.compile(r"\bGETDATE\s*\(", re.I), "GETDATE() (use SYSDATE()/CURRENT_TIMESTAMP())"),
    (re.compile(r"\bMONEY\b", re.I), "MONEY type (use NUMBER(19,4))"),
    (re.compile(r"^\s*PRINT\b", re.I | re.M), "PRINT"),
    (re.compile(r"\bRAISERROR\b", re.I), "RAISERROR (use RAISE)"),
    (re.compile(r"\bBEGIN\s+TRY\b|\bBEGIN\s+CATCH\b", re.I), "TRY/CATCH (use EXCEPTION)"),
    (re.compile(r"@@\w+"), "@@ system variable"),
    (re.compile(r"\(\s*MAX\s*\)", re.I), "(MAX) length"),
]


@pytest.mark.parametrize("path", SQL_FILES, ids=_ids(SQL_FILES))
def test_sql_has_no_templating_variables(path):
    code = strip_sql(path.read_text(encoding="utf-8"))
    for pattern, label in TEMPLATING:
        match = pattern.search(code)
        assert not match, f"{label} found: {match.group(0)!r} (PLAN.md §3 forbids templating in committed SQL)"


@pytest.mark.parametrize("path", SQL_FILES, ids=_ids(SQL_FILES))
def test_sql_has_no_tsql_leftovers(path):
    code = strip_sql(path.read_text(encoding="utf-8"))
    for pattern, label in TSQL_LEFTOVERS:
        match = pattern.search(code)
        assert not match, f"T-SQL construct left in Snowflake SQL: {label} ({match.group(0).strip()!r})"


@pytest.mark.parametrize("path", SNOWFLAKE_FILES, ids=_ids(SNOWFLAKE_FILES))
def test_snowflake_sql_has_header_comment(path):
    first = first_code_line(path.read_text(encoding="utf-8"))
    assert first.startswith(("--", "/*")), "every .sql under migration/snowflake must start with a header comment"


@pytest.mark.parametrize("path", SNOWFLAKE_FILES, ids=_ids(SNOWFLAKE_FILES))
def test_snowflake_sql_uses_contract_database(path):
    code = strip_sql(path.read_text(encoding="utf-8"))
    assert re.search(r"\bUSE\s+DATABASE\s+BANKING_DWH\b", code, re.I), "PLAN.md §3: deploy scripts USE DATABASE BANKING_DWH"


_SECRET_KEYWORDS = r"PASSWORD|PASSWD|AWS_SECRET_KEY|AWS_KEY_ID|AWS_TOKEN|AZURE_SAS_TOKEN|MASTER_KEY|PRIVATE_KEY|PASSPHRASE|SECRET|TOKEN"
# key = 'value' / key: "value" where key names a secret (not a *_PATH/*_FILE pointer) and the value is a
# whitespace-free literal. Values with spaces are code/prose, not credentials; this keeps false positives low.
CREDENTIAL_ASSIGNMENT = re.compile(
    rf"\b[\w.]*(?:{_SECRET_KEYWORDS})(?![\w]*(?:_PATH|_FILE)\b)[\w]*[\"']?\s*(?:=|:)\s*([\"'])(\S*?)\1",
    re.I,
)
CREDENTIAL_TOKENS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM private key"),
    (re.compile(r"AccountKey=[A-Za-z0-9+/=]{20,}"), "Azure storage account key"),
    (re.compile(r"[?&]sig=[A-Za-z0-9%+/=]{20,}"), "Azure SAS signature"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "GitHub token"),
]
PLACEHOLDER = re.compile(
    r"^(|\*+|x{3,}|<[^>]*>|\$\{\{.*\}\}|\$\{.*\}|\$[A-Z_][A-Z0-9_]*|[A-Z][A-Z0-9_]*|.*(changeme|change_me|redacted|"
    r"replace|placeholder|example|dummy|your[_-]).*)$",
    re.I,
)


def find_credentials(text: str) -> list[str]:
    hits = [label for pattern, label in CREDENTIAL_TOKENS if pattern.search(text)]
    for match in CREDENTIAL_ASSIGNMENT.finditer(text):
        value = match.group(2)
        if not PLACEHOLDER.match(value):
            hits.append(f"credential literal: {match.group(0)[:60]!r}")
    return hits


SCANNED = scanned_text_files()


@pytest.mark.parametrize("path", SCANNED, ids=_ids(SCANNED))
def test_no_committed_credentials(path):
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="latin-1")
    assert not find_credentials(text)


@pytest.mark.parametrize(
    "sample",
    [
        "CREATE STAGE s URL='azure://a/b' CREDENTIALS = (AZURE_SAS_TOKEN = '" + "sv=2022&sig=abc123')",
        "conn = connect(pass" + "word='Hunter2!')",
        "-----BEGIN " + "PRIVATE KEY-----",
        "SNOWFLAKE_PRIVATE_KEY_PASS" + "PHRASE: 'p@ssw0rd'",
        "DefaultEndpointsProtocol=https;AccountName=x;Account" + "Key=" + "A" * 40,
    ],
)
def test_credential_scanner_detects(sample):
    assert find_credentials(sample)


@pytest.mark.parametrize(
    "sample",
    [
        "password=os.environ['SNOWFLAKE_PASSWORD']",
        "SNOWFLAKE_PASSWORD: ${{ secrets.SNOWFLAKE_PASSWORD }}",
        "CREDENTIALS = (AZURE_SAS_TOKEN = '<sas-token>')",
        '"password": "SNOWFLAKE_PASSWORD"',
        "password = ''",
    ],
)
def test_credential_scanner_allows_placeholders(sample):
    assert not find_credentials(sample)


def test_strip_sql_blanks_comments_strings_and_non_sql_bodies():
    sql = (
        "-- GO\n/* ISNULL( */ SELECT 'GETDATE()' AS x, a || 'it''s' FROM t; -- {{ x }}\n"
        "CREATE PROCEDURE p() RETURNS VARCHAR LANGUAGE PYTHON RUNTIME_VERSION = '3.10' HANDLER = 'h'\n"
        "AS $$\nprint('@@x')\n$$;\n"
        "CREATE PROCEDURE q() RETURNS VARCHAR LANGUAGE SQL AS $$ BEGIN RETURN ISNULL(1, 2); END; $$;"
    )
    code = strip_sql(sql)
    assert "GO" not in code.split("SELECT")[0]
    assert "GETDATE" not in code and "{{" not in code and "it''s" not in code and "@@" not in code
    assert "ISNULL(1, 2)" in code  # SQL procedure bodies are still checked
    assert len(code.splitlines()) == len(sql.splitlines())
