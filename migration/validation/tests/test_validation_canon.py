"""Canonicalisation rules (README.md) that Python and the generated Snowflake SQL must share."""

import datetime as dt
from decimal import Decimal

import pytest

from _validation_helpers import validate as v


@pytest.mark.parametrize(
    "value, expected",
    [(1, "1"), ("25", "25"), (Decimal("1500000.0000"), "1500000"), (Decimal("-0"), "0"), (Decimal("2.5"), "3"),
     (Decimal("-2.5"), "-3"), (None, None)],
)
def test_canon_int(value, expected):
    assert v.canon_int(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [(1500000, "1500000.0000"), ("1500000.0000", "1500000.0000"), (Decimal("0.00005"), "0.0001"),
     (Decimal("-0.00004"), "0.0000"), ("-50000", "-50000.0000"), (0.1, "0.1000"), (None, None)],
)
def test_canon_dec4(value, expected):
    assert v.canon_dec4(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [("2022-01-01 09:10:00", "2022-01-01 09:10:00.000"), ("2022-01-01 09:10:00.000", "2022-01-01 09:10:00.000"),
     (dt.datetime(2024, 1, 18, 13, 10, 0, 999_999), "2024-01-18 13:10:00.999"), (dt.date(2020, 5, 1),
                                                                                 "2020-05-01 00:00:00.000"),
     (None, None)],
)
def test_canon_ts_truncates_to_milliseconds(value, expected):
    assert v.canon_ts(value) == expected


def test_canon_date_truncates_timestamps():
    assert v.canon_date("2020-05-01 09:00:00") == "2020-05-01"
    assert v.canon_date(dt.date(2020, 5, 1)) == "2020-05-01"


def test_canon_str_is_verbatim():
    assert v.canon_str(" Shelly ") == " Shelly "
    assert v.canon_str("") == ""
    assert v.canon_str(None) is None


def test_null_and_empty_string_hash_differently():
    assert v.canon_row([None, "a"], ["str", "str"]) != v.canon_row(["", "a"], ["str", "str"])
    assert v.canon_row([None], ["str"]) == "\x1e"


def test_row_string_uses_unit_separator():
    assert v.canon_row([1, "x", None, "1.5"], ["int", "str", "str", "dec4"]) == "1\x1fx\x1f\x1e\x1f1.5000"


def test_multiset_checksum_is_order_independent_but_counts_duplicates():
    hashes = [v.sha256_hex(str(i)) for i in range(5)]
    assert v.multiset_checksum(hashes) == v.multiset_checksum(list(reversed(hashes)))
    assert v.multiset_checksum(hashes) != v.multiset_checksum(hashes + hashes[:1])
    assert v.multiset_checksum([]) == v.sha256_hex("")


def test_sample_threshold_and_membership():
    assert v.sample_threshold(0.5) == "8000"
    assert v.sample_threshold(0.01) == "028f"
    with pytest.raises(ValueError):
        v.sample_threshold(1.0)
    keys = [v.canon_row([i], ["int"]) for i in range(10_000)]
    picked = [k for k in keys if v.in_sample(v.sha256_hex(k), "028f")]
    assert 50 < len(picked) < 150  # ~1 %, deterministic
    assert picked == [k for k in keys if v.in_sample(v.sha256_hex(k), "028f")]


def test_excel_serial_rounds_float_noise():
    assert v.excel_serial_to_datetime(Decimal("45309.548611111109")) == dt.datetime(2024, 1, 18, 13, 10)
    assert v.excel_serial_to_datetime(45312.333333333336) == dt.datetime(2024, 1, 21, 8, 0)
