"""README and PLAN.md §4 layout contract checks (offline)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from repo_contract import (
    MIGRATION,
    README,
    REPO_ROOT,
    WORKFLOW,
    github_slug,
    markdown_links,
    plan_env_vars,
    plan_layout,
)

README_TEXT = README.read_text(encoding="utf-8")
LAYOUT = plan_layout()
LAYOUT_DIRS = [p for p in LAYOUT if p.endswith("/")]


def _resolve(link: str) -> Path:
    target = link.split("#", 1)[0]
    return (README.parent / target).resolve()


def _relative_links() -> list[str]:
    return sorted({ln for ln in markdown_links(README_TEXT) if not re.match(r"^(https?:|mailto:|#)", ln)})


def _under_layout(path: Path) -> bool:
    for entry in LAYOUT:
        layout_path = (REPO_ROOT / entry).resolve()
        if path == layout_path or layout_path in path.parents:
            return True
    return False


def test_plan_layout_is_parsed():
    assert "migration/README.md" in LAYOUT
    assert "migration/snowflake/ddl/" in LAYOUT
    assert "migration/tests/" in LAYOUT
    assert ".github/workflows/snowflake-migration.yml" in LAYOUT


@pytest.mark.parametrize(
    "layout_path",
    [p for p in LAYOUT if p.startswith("migration/") and p not in ("migration/", "migration/snowflake/")],
)
def test_readme_links_every_layout_path(layout_path):
    """Every unit directory/file from PLAN.md §4 is linked from the README."""
    linked = {_resolve(link) for link in _relative_links()}
    assert (REPO_ROOT / layout_path).resolve() in linked, f"README does not link {layout_path}"


def test_readme_links_workflow():
    assert WORKFLOW.resolve() in {_resolve(link) for link in _relative_links()}


@pytest.mark.parametrize("link", _relative_links())
def test_readme_relative_link_resolves(link):
    """Relative links must resolve; links into another unit's §4 directory may be pending until it merges."""
    target = _resolve(link)
    if target.exists():
        return
    assert _under_layout(target), f"README link {link!r} points outside the PLAN.md §4 layout and does not exist"
    pytest.skip(f"{link} belongs to a unit that has not merged yet")


@pytest.mark.parametrize("layout_path", LAYOUT)
def test_layout_path_has_expected_kind_once_present(layout_path):
    path = REPO_ROOT / layout_path
    if not path.exists():
        pytest.skip(f"{layout_path} not present yet")
    if layout_path.endswith("/"):
        assert path.is_dir(), f"{layout_path} should be a directory"
    else:
        assert path.is_file(), f"{layout_path} should be a file"


def test_present_unit_directories_are_not_empty():
    for layout_dir in LAYOUT_DIRS:
        path = REPO_ROOT / layout_dir
        if path.is_dir():
            assert any(path.iterdir()), f"{layout_dir} exists but is empty"


def test_readme_anchor_links_match_headings():
    headings = {github_slug(h) for h in re.findall(r"^#{1,6} (.+)$", README_TEXT, flags=re.M)}
    anchors = {ln.split("#", 1)[1] for ln in markdown_links(README_TEXT) if ln.startswith("#")}
    missing = sorted(anchors - headings)
    assert not missing, f"README anchors without a heading: {missing}"


@pytest.mark.parametrize(
    "heading",
    ["Layout", "Deploy order", "Cutover runbook", "Rollback plan", "Known semantic differences",
     "Running validation locally and live", "CI"],
)
def test_readme_has_required_sections(heading):
    assert re.search(rf"^## {re.escape(heading)}$", README_TEXT, flags=re.M), f"missing section: {heading}"


def _section(title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}$(.*?)(?=^## |\Z)", README_TEXT, flags=re.M | re.S)
    assert match, title
    return match.group(1)


@pytest.mark.parametrize(
    "step",
    ["Freeze", "Final Synapse extract", "CETAS", "Stage", "Deploy", "Initial load", "Validate", "Parallel run",
     "Switch consumers", "Resume tasks", "Decommission"],
)
def test_runbook_covers_step(step):
    assert step in _section("Cutover runbook")


def test_every_runbook_step_has_a_rollback():
    runbook_steps = re.findall(r"^\| (\d+)\. ", _section("Cutover runbook"), flags=re.M)
    rollback_steps = re.findall(r"^\| (\d+)\. ", _section("Rollback plan"), flags=re.M)
    assert runbook_steps and runbook_steps == rollback_steps


@pytest.mark.parametrize(
    "topic",
    [
        "ILIKE",  # case-insensitive collation vs case-sensitive
        "not enforced",  # FKs
        "OPS.FACTTRANSACTION_REJECTS",
        "DIE_ON_ERROR=false",
        "DB > EXCEL > CSV",
        "sign-off",
        "NUMBER(19,4)",
        "TIMESTAMP_NTZ",
        "TIMEZONE",
        "`'a' + 'b'`",
        "\\|\\|",
        "RETURNS TABLE",
        "ORDER BY",
        "BEGIN TRY",
        "EXCEPTION",
        "IDENTITY",
        "not gap-free",
        "without a `GO`",
    ],
)
def test_semantic_differences_cover_topic(topic):
    assert topic in _section("Known semantic differences")


@pytest.mark.parametrize("env_var", plan_env_vars())
def test_readme_documents_plan_env_var(env_var):
    assert env_var in _section("Running validation locally and live")


def test_readme_documents_deploy_order_from_plan():
    deploy = _section("Deploy order")
    positions = [deploy.find(d) for d in ("snowflake/ddl/", "snowflake/loads/", "snowflake/procedures/",
                                          "CALL OPS.SP_LOAD_", "validate.py")]
    assert -1 not in positions and positions == sorted(positions), "deploy order must follow PLAN.md §4"


def test_readme_load_order_matches_talend_run_order():
    deploy = _section("Deploy order")
    order = [deploy.find(f"OPS.SP_LOAD_{t}") for t in ("DIMBRANCH", "DIMACCOUNT", "DIMCUSTOMER", "FACTTRANSACTION")]
    assert -1 not in order and order == sorted(order)


def test_readme_expected_counts_match_baseline():
    deploy = _section("Deploy order")
    for table in ("DimBranch", "DimAccount", "DimCustomer", "FactTransaction"):
        rows = (MIGRATION / "baseline" / f"{table}.csv").read_text(encoding="utf-8").strip().splitlines()
        assert f"`{table}` {len(rows) - 1}" in deploy, f"README row count for {table} != baseline"
