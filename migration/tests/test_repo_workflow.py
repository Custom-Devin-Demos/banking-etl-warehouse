"""Contract checks for .github/workflows/snowflake-migration.yml (offline; runs its shell steps with stubs)."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from repo_contract import REPO_ROOT, WORKFLOW, plan_env_vars, plan_sqlfluff_pin

yaml = pytest.importorskip("yaml")

WF_TEXT = WORKFLOW.read_text(encoding="utf-8")
WF = yaml.safe_load(WF_TEXT)
JOBS = WF["jobs"]
BASH = shutil.which("bash")


def _triggers():
    return WF.get("on", WF.get(True))  # PyYAML (YAML 1.1) parses the bare key `on` as True


def _steps(job):
    return JOBS[job]["steps"]


def _run_text(job):
    return "\n".join(s.get("run", "") for s in _steps(job))


def _step(job, needle):
    for step in _steps(job):
        if needle in step.get("run", ""):
            return step
    raise AssertionError(f"{job}: no step running {needle!r}")


def test_triggers_on_pull_request_and_push_to_main():
    triggers = _triggers()
    assert "pull_request" in triggers
    assert "main" in triggers["push"]["branches"]


def test_all_actions_pinned_to_commit_sha():
    uses = re.findall(r"uses:\s*(\S+)", WF_TEXT)
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), f"action not pinned to a commit SHA: {ref}"


def test_lint_job_uses_pinned_sqlfluff_and_lints_migration():
    assert WF["env"]["SQLFLUFF_VERSION"] == plan_sqlfluff_pin()
    run = _run_text("sqlfluff-lint")
    assert 'sqlfluff==${SQLFLUFF_VERSION}' in run
    assert re.search(r"^sqlfluff lint migration/$", run, flags=re.M)


def test_sqlfluff_config_is_snowflake_and_ignores_legacy_tsql():
    config = (REPO_ROOT / ".sqlfluff").read_text(encoding="utf-8")
    assert re.search(r"^dialect\s*=\s*snowflake$", config, flags=re.M)
    assert re.search(r"^templater\s*=\s*raw$", config, flags=re.M)
    ignore = (REPO_ROOT / ".sqlfluffignore").read_text(encoding="utf-8")
    assert "sql_scripts/" in ignore and "migration/baseline/" in ignore


def test_offline_tests_job_installs_unit_requirements_and_runs_pytest():
    run = _run_text("offline-tests")
    assert "requirements*.txt" in run
    assert "pytest migration" in run
    assert _step("offline-tests", "pytest migration")["env"]["SNOWFLAKE_ACCOUNT"] == ""


def test_live_job_is_gated_on_snowflake_account_secret():
    gate = JOBS["live-check-gate"]
    assert "secrets.SNOWFLAKE_ACCOUNT" in str(gate)
    live = JOBS["live-validation"]
    assert "live-check-gate" in live["needs"]
    assert live["if"] == "needs.live-check-gate.outputs.enabled == 'true'"
    assert "python migration/validation/validate.py" in _run_text("live-validation")


@pytest.mark.parametrize("env_var", plan_env_vars())
def test_live_job_maps_plan_env_var(env_var):
    live = JOBS["live-validation"]
    if env_var == "SNOWFLAKE_PRIVATE_KEY_PATH":
        assert "SNOWFLAKE_PRIVATE_KEY_PATH=" in _run_text("live-validation")  # written to $GITHUB_ENV from a secret
    else:
        assert env_var in live["env"], f"{env_var} not mapped in live-validation env"
        assert f"secrets.{env_var}" in live["env"][env_var]


def test_only_live_jobs_read_secrets():
    for name, job in JOBS.items():
        if name not in ("live-check-gate", "live-validation"):
            assert "secrets." not in str(job), f"offline job {name} must not read secrets"


# --- Execute the workflow's shell logic with stubs (GitHub's default `bash -e` shell) ------------------------


def _run_bash(script: str, env: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess:
    full_env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    full_env.update(env)
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env=full_env, cwd=tmp_path, capture_output=True, text=True, check=False,
    )


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("pytest_rc,expected_rc", [(0, 0), (5, 0), (1, 1), (2, 2)])
def test_pytest_step_treats_no_tests_collected_as_success(tmp_path, pytest_rc, expected_rc):
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "python"
    stub.write_text(f"#!/bin/sh\nexit {pytest_rc}\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    script = _step("offline-tests", "pytest migration")["run"]
    result = _run_bash(script, {"PATH": f"{stub_dir}:{os.environ['PATH']}"}, tmp_path)
    assert result.returncode == expected_rc, result.stdout + result.stderr


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("account,enabled", [("", "false"), ("myorg-myaccount", "true")])
def test_gate_step_output(tmp_path, account, enabled):
    output = tmp_path / "github_output"
    output.write_text("")
    script = JOBS["live-check-gate"]["steps"][0]["run"]
    result = _run_bash(script, {"SNOWFLAKE_ACCOUNT": account, "GITHUB_OUTPUT": str(output)}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert output.read_text().strip() == f"enabled={enabled}"
