"""Offline checks (what CI runs): Talend rules applied to the extracts reproduce migration/baseline/."""

import json
import shutil

import pytest

from _validation_helpers import validate as v

SRC = v.Sources()
COMPUTED = v.expected_transformed(SRC)


@pytest.mark.parametrize("spec", v.DWH_SPECS + v.PROC_SPECS, ids=lambda s: s.name)
def test_transform_reproduces_baseline(spec):
    baseline = SRC.baseline(spec)
    assert v.compute_metrics(spec, COMPUTED[spec.name]) == v.compute_metrics(spec, baseline)
    assert sorted(v.row_hashes(spec, COMPUTED[spec.name])) == sorted(v.row_hashes(spec, baseline))


@pytest.mark.parametrize(
    "name, rows", [("RAW.STATE", 9), ("RAW.CITY", 52), ("RAW.CUSTOMER", 20), ("RAW.ACCOUNT", 21), ("RAW.BRANCH", 5),
                   ("RAW.TRANSACTION_DB", 10), ("RAW.TRANSACTION_EXCEL", 7), ("RAW.TRANSACTION_CSV", 12)],
)
def test_raw_sources_match_plan_inventory(name, rows):
    assert len(SRC.raw(name)) == rows


def test_rejects_are_orphans_plus_lower_precedence_duplicates():
    rejects = COMPUTED["OPS.FACTTRANSACTION_REJECTS"]
    by_reason = {}
    for r in rejects:
        by_reason.setdefault(r[7], []).append((int(r[0]), r[6]))
    assert sorted(by_reason["ORPHAN_ACCOUNT"]) == [(23, "CSV"), (24, "CSV"), (25, "CSV")]
    assert sorted(by_reason["DUPLICATE_LOWER_PRECEDENCE"]) == [(6, "EXCEL"), (7, "EXCEL"), (14, "CSV"), (15, "CSV")]
    assert set(by_reason) == {"ORPHAN_ACCOUNT", "DUPLICATE_LOWER_PRECEDENCE"}
    spec = v.OPS_SPECS[0]
    m = v.compute_metrics(spec, rejects)
    assert m["ROW_COUNT"] == "7"
    assert m["SUM_TRANSACTION_ID"] == "114.0000"  # 23+24+25 + 6+7+14+15
    assert m["SUM_AMOUNT"] == "2950000.0000"  # 100k+300k+400k + 50k+100k+1.5M+500k


def test_db_wins_over_excel_for_conflicting_duplicates():
    fact = {int(r[0]): r for r in COMPUTED["DWH.FACTTRANSACTION"]}
    assert v.canon_ts(fact[6][2]) == "2022-02-21 13:10:00.000"
    assert v.canon_ts(fact[7][2]) == "2022-02-21 14:00:00.000"


def test_balance_per_customer_is_case_insensitive():
    rows = COMPUTED["DWH.SP_BALANCEPERCUSTOMER('shelly')"]
    assert {r[0] for r in rows} == {"SHELLY JUWITA"}


def test_stdlib_xlsx_reader_matches_openpyxl():
    openpyxl = pytest.importorskip("openpyxl")
    ws = openpyxl.load_workbook(v.DEFAULT_DATA_DIR / "transaction_excel.xlsx", read_only=True)["Sheet1"]
    expected = [r for r in ws.iter_rows(min_row=2, values_only=True)]
    spec = next(s for s in v.RAW_SPECS if s.name == "RAW.TRANSACTION_EXCEL")
    assert v.row_hashes(spec, SRC.raw(spec.name)) == v.row_hashes(spec, expected)


def test_offline_cli_passes_and_writes_report(tmp_path, capsys):
    report = tmp_path / "report.json"
    assert v.main(["--offline", "--report", str(report)]) == 0
    data = json.loads(report.read_text())
    assert data["passed"] is True and data["mode"] == "offline"
    assert data["summary"]["failed"] == 0 and data["summary"]["checks"] >= 18
    assert "RESULT: PASS" in capsys.readouterr().out


def test_offline_cli_fails_on_tampered_baseline(tmp_path, capsys):
    baseline = tmp_path / "baseline"
    shutil.copytree(v.DEFAULT_BASELINE_DIR, baseline)
    fact = baseline / "FactTransaction.csv"
    fact.write_text(fact.read_text().replace("22,21,2024-01-22 10:45:00.000,800000.0000",
                                             "22,21,2024-01-22 10:45:00.000,800001.0000"))
    report = tmp_path / "report.json"
    assert v.main(["--offline", "--baseline-dir", str(baseline), "--report", str(report)]) == 1
    data = json.loads(report.read_text())
    failed = {c["name"]: c for c in data["checks"] if not c["ok"]}
    assert "DWH.FACTTRANSACTION" in failed
    assert failed["DWH.FACTTRANSACTION"]["row_diff"]["changed"] == ["22"]
    assert "RESULT: FAIL" in capsys.readouterr().out
