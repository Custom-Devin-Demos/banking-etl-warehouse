"""Live-mode plumbing. Real Snowflake tests run only when SNOWFLAKE_ACCOUNT is set."""

import json
import os
import subprocess
import sys

import pytest

from _validation_helpers import VALIDATION_DIR
from _validation_helpers import validate as v

LIVE = pytest.mark.skipif(not os.environ.get("SNOWFLAKE_ACCOUNT"), reason="SNOWFLAKE_ACCOUNT not set")


def _env_without_snowflake():
    return {k: val for k, val in os.environ.items() if not k.startswith("SNOWFLAKE_")}


def test_connector_is_imported_lazily():
    code = f"import sys; sys.path.insert(0, {str(VALIDATION_DIR)!r}); import validate; " \
           "assert 'snowflake.connector' not in sys.modules; validate.main(['--offline'])" \
           "; assert 'snowflake.connector' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True, env=_env_without_snowflake(), capture_output=True)


def test_live_mode_skips_cleanly_without_account():
    cmd = [sys.executable, str(VALIDATION_DIR / "validate.py")]
    res = subprocess.run(cmd, env=_env_without_snowflake(), capture_output=True, text=True)
    assert res.returncode == 0 and "skipped" in res.stderr
    res = subprocess.run(cmd + ["--require-live"], env=_env_without_snowflake(), capture_output=True, text=True)
    assert res.returncode == 2


def test_missing_auth_is_a_config_error(monkeypatch):
    for k in ("SNOWFLAKE_PASSWORD", "SNOWFLAKE_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(v.ConfigError):
        v.connection_params(v.parse_args(["--account", "xy12345", "--user", "svc"]))


def test_password_and_cli_overrides(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "pw")
    monkeypatch.setenv("SNOWFLAKE_WAREHOUSE", "WH_ENV")
    monkeypatch.delenv("SNOWFLAKE_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("SNOWFLAKE_DATABASE", raising=False)
    p = v.connection_params(v.parse_args(["--account", "xy12345", "--user", "svc", "--role", "R"]))
    assert p["password"] == "pw" and p["warehouse"] == "WH_ENV" and p["role"] == "R"
    assert p["database"] == "BANKING_DWH"


def test_key_pair_auth(monkeypatch, tmp_path):
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.BestAvailableEncryption(b"secret"))
    path = tmp_path / "rsa_key.p8"
    path.write_bytes(pem)
    monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", "secret")
    p = v.connection_params(v.parse_args(["--account", "a", "--user", "u", "--private-key-path", str(path)]))
    assert "password" not in p and isinstance(p["private_key"], bytes)


class FakeCursor:
    def __init__(self, cols, rows):
        self.description = [(c,) for c in cols]
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeSnowflake:
    """Answers the committed SQL files with metrics computed in Python from ``tables``."""

    def __init__(self, tables):
        self.tables = tables
        self.by_sql = {}
        for spec in v.ALL_SPECS:
            m, r = v.sql_paths(spec)
            self.by_sql[m.read_text()] = (spec, "metrics")
            self.by_sql[r.read_text()] = (spec, "rowhashes")
        self.executed = []

    def execute_string(self, sql):
        spec, kind = self.by_sql[sql]
        self.executed.append((spec.name, kind))
        if spec.name not in self.tables:
            raise RuntimeError(f"Object '{spec.name}' does not exist or not authorized.")
        rows = self.tables[spec.name]
        if kind == "metrics":
            m = v.compute_metrics(spec, rows)
            return [FakeCursor(list(m), [tuple(m.values())])]
        return [FakeCursor(["ROW_KEY", "ROW_HASH"], v.row_hashes(spec, rows))]


def test_run_live_passes_against_matching_warehouse():
    tables = v.expected_live(v.Sources())
    results = v.run_live(FakeSnowflake(tables), v.Sources())
    assert [r.name for r in results if not r.ok] == []
    assert len(results) == len(v.ALL_SPECS)


def test_run_live_reports_mismatch_diff_and_missing_objects():
    tables = v.expected_live(v.Sources())
    fact = [list(r) for r in tables["DWH.FACTTRANSACTION"]]
    fact[0][3] = "100001.0000"
    tables["DWH.FACTTRANSACTION"] = [tuple(r) for r in fact[:-1]]  # one changed, one missing
    del tables["OPS.FACTTRANSACTION_REJECTS"]
    fake = FakeSnowflake(tables)
    results = {r.name: r for r in v.run_live(fake, v.Sources())}
    fact_res = results["DWH.FACTTRANSACTION"]
    assert not fact_res.ok
    assert fact_res.row_diff == {"missing_in_actual": ["22"], "unexpected_in_actual": [], "changed": ["1"]}
    assert ("DWH.FACTTRANSACTION", "rowhashes") in fake.executed
    assert ("DWH.DIMBRANCH", "rowhashes") not in fake.executed  # diagnostics only on mismatch
    assert "does not exist" in results["OPS.FACTTRANSACTION_REJECTS"].error
    report = v.build_report("live", list(results.values()))
    assert report["passed"] is False and report["summary"]["failed"] == 2
    assert "FAIL  DWH.FACTTRANSACTION" in v.render_summary(report)
    json.dumps(report)


def test_main_live_exit_codes(monkeypatch, tmp_path):
    tables = v.expected_live(v.Sources())
    monkeypatch.setattr(v, "connect", lambda params: type("C", (FakeSnowflake,), {"close": lambda self: None})(tables))
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "pw")
    out = tmp_path / "r.json"
    assert v.main(["--account", "a", "--user", "u", "--report", str(out), "--only", "DWH."]) == 0
    assert {c["name"] for c in json.loads(out.read_text())["checks"]} == {s.name for s in v.DWH_SPECS} | {
        s.name for s in v.PROC_SPECS}
    tables["DWH.DIMBRANCH"] = tables["DWH.DIMBRANCH"][1:]
    assert v.main(["--account", "a", "--user", "u", "--only", "DWH.DIMBRANCH"]) == 1


@LIVE
def test_live_snowflake_matches_baseline(tmp_path):
    """End-to-end against a real account (CI live job). Requires the DDL, loads and procedures deployed."""
    report = tmp_path / "live_report.json"
    rc = v.main(["--report", str(report)])
    assert rc == 0, v.render_summary(json.loads(report.read_text()))
