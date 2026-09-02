"""Unit tests for units/hashing (Manual §72, §73)."""
import pytest
from rca.utils.units import (
    freq_mhz_to_period_ns, from_seconds, parse_frequency_string, parse_time_string,
    period_ns_to_freq_mhz, to_seconds,
)
from rca.utils.hashing import stable_hash, hash_file
from pathlib import Path


def test_time_conversion_ns():
    s = to_seconds(10, "ns")
    assert s == pytest.approx(10e-9)
    assert from_seconds(s, "ns") == pytest.approx(10.0)


def test_parse_time_string():
    assert parse_time_string("10ns") == pytest.approx(10e-9)
    assert parse_time_string("2.5 ns") == pytest.approx(2.5e-9)
    assert parse_time_string("100ps") == pytest.approx(100e-12)
    assert parse_time_string("5") == pytest.approx(5e-9)


def test_parse_freq_string():
    assert parse_frequency_string("100MHz") == pytest.approx(100e6)
    assert parse_frequency_string("2.5 GHz") == pytest.approx(2.5e9)


def test_period_freq_conversion():
    assert period_ns_to_freq_mhz(10) == pytest.approx(100.0)
    assert freq_mhz_to_period_ns(100) == pytest.approx(10.0)


def test_stable_hash_determinism():
    assert stable_hash({"a": 1, "b": 2}) == stable_hash({"b": 2, "a": 1})
    assert stable_hash({"a": 1}) != stable_hash({"a": 2})


def test_hash_file(tmp_path):
    p = tmp_path / "x.v"
    p.write_text("module x(); endmodule")
    h = hash_file(p)
    assert isinstance(h, str) and len(h) == 64
