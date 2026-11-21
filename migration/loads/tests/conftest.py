from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("duckdb")
pytest.importorskip("openpyxl")

import loads_duckdb_mirror as mirror  # noqa: E402
import xlsx_to_csv  # noqa: E402


@pytest.fixture(scope="session")
def excel_csv(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("excel") / "transaction_excel.csv"
    xlsx_to_csv.convert(mirror.DATA / "transaction_excel.xlsx", out)
    return out


@pytest.fixture()
def con():
    connection = mirror.new_connection()
    yield connection
    connection.close()
