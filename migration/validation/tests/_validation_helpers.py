"""Shared helpers for the validation tests (imported by path, no package needed)."""

import sys
from pathlib import Path

VALIDATION_DIR = Path(__file__).resolve().parents[1]
if str(VALIDATION_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATION_DIR))

import validate  # noqa: E402,F401
