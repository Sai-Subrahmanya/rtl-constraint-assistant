"""
Unit handling and conversion utilities (Manual §72, §73).

All internal time values are stored in **seconds** (SI) as floats.
User-facing helpers convert to/from ns/ps/fs and MHz/GHz.
"""

from __future__ import annotations

import math
from typing import Union

from .enums import FrequencyUnit, TimeUnit

# Conversion factors TO seconds
_TIME_TO_S: dict[TimeUnit, float] = {
    TimeUnit.SECOND: 1.0,
    TimeUnit.NANOSECOND: 1e-9,
    TimeUnit.PICOSECOND: 1e-12,
    TimeUnit.FEMTOSECOND: 1e-15,
}

# Conversion factors TO Hz
_FREQ_TO_HZ: dict[FrequencyUnit, float] = {
    FrequencyUnit.HERTZ: 1.0,
    FrequencyUnit.KILOHERTZ: 1e3,
    FrequencyUnit.MEGAHERTZ: 1e6,
    FrequencyUnit.GIGAHERTZ: 1e9,
}


Number = Union[int, float]


def to_seconds(value: Number, unit: TimeUnit | str) -> float:
    """Convert a time value in ``unit`` to internal seconds representation."""
    u = TimeUnit(unit) if isinstance(unit, str) else unit
    return float(value) * _TIME_TO_S[u]


def from_seconds(seconds: Number, unit: TimeUnit | str) -> float:
    """Convert an internal seconds value to ``unit``."""
    u = TimeUnit(unit) if isinstance(unit, str) else unit
    return float(seconds) / _TIME_TO_S[u]


def to_hz(value: Number, unit: FrequencyUnit | str) -> float:
    """Convert a frequency value in ``unit`` to internal Hz representation."""
    u = FrequencyUnit(unit) if isinstance(unit, str) else unit
    return float(value) * _FREQ_TO_HZ[u]


def from_hz(hz: Number, unit: FrequencyUnit | str) -> float:
    """Convert an internal Hz value to ``unit``."""
    u = FrequencyUnit(unit) if isinstance(unit, str) else unit
    return float(hz) / _FREQ_TO_HZ[u]


# --- Period <-> frequency ---------------------------------------------------

def period_to_frequency(period_s: Number) -> float:
    """Convert period in seconds to frequency in Hz."""
    if float(period_s) <= 0:
        raise ValueError(f"Period must be positive, got {period_s}")
    return 1.0 / float(period_s)


def frequency_to_period(hz: Number) -> float:
    """Convert frequency in Hz to period in seconds."""
    if float(hz) <= 0:
        raise ValueError(f"Frequency must be positive, got {hz}")
    return 1.0 / float(hz)


def period_ns_to_freq_mhz(period_ns: Number) -> float:
    """Manual §73: period_ns = 1000 / frequency_MHz."""
    return 1000.0 / float(period_ns)


def freq_mhz_to_period_ns(freq_mhz: Number) -> float:
    """Manual §73: frequency_MHz = 1000 / period_ns."""
    return 1000.0 / float(freq_mhz)


# --- Pretty formatting ------------------------------------------------------

def format_time(seconds: Number, precision: int = 3) -> str:
    """Format a seconds value as ns with the given precision."""
    return f"{from_seconds(seconds, TimeUnit.NANOSECOND):.{precision}f} ns"


def format_frequency(hz: Number, precision: int = 3) -> str:
    """Format a Hz value as MHz with the given precision."""
    return f"{from_hz(hz, FrequencyUnit.MEGAHERTZ):.{precision}f} MHz"


def parse_time_string(s: str) -> float:
    """Parse strings like '10ns', '2.5 ns', '100ps' into seconds.

    Defaults to nanoseconds if no unit suffix is provided.
    """
    s = s.strip().lower().replace(" ", "")
    unit_map: dict[str, TimeUnit] = {
        "ns": TimeUnit.NANOSECOND,
        "ps": TimeUnit.PICOSECOND,
        "fs": TimeUnit.FEMTOSECOND,
        "s": TimeUnit.SECOND,
    }
    # Try longer suffixes first so "ns" matches before "s".
    for suf, u in sorted(unit_map.items(), key=lambda kv: -len(kv[0])):
        if s.endswith(suf):
            val = float(s[: -len(suf)])
            return to_seconds(val, u)
    # No unit — assume ns (common in EDA)
    return to_seconds(float(s), TimeUnit.NANOSECOND)


def parse_frequency_string(s: str) -> float:
    """Parse strings like '100MHz', '2.5 GHz', '1GHz' into Hz.

    Defaults to MHz if no unit suffix is provided.
    """
    s = s.strip().lower().replace(" ", "")
    unit_map: dict[str, FrequencyUnit] = {
        "khz": FrequencyUnit.KILOHERTZ,
        "mhz": FrequencyUnit.MEGAHERTZ,
        "ghz": FrequencyUnit.GIGAHERTZ,
        "hz": FrequencyUnit.HERTZ,
    }
    for suf, u in sorted(unit_map.items(), key=lambda kv: -len(kv[0])):
        if s.endswith(suf):
            val = float(s[: -len(suf)])
            return to_hz(val, u)
    # No unit — assume MHz
    return to_hz(float(s), FrequencyUnit.MEGAHERTZ)


def nearly_equal(a: Number, b: Number, rel_tol: float = 1e-9, abs_tol: float = 1e-18) -> bool:
    """Floating-point comparison with absolute tolerance in seconds."""
    return math.isclose(float(a), float(b), rel_tol=rel_tol, abs_tol=abs_tol)
