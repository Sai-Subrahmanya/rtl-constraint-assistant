"""RCA utility modules."""

from .enums import *  # re-export enums for convenience
from .hashing import hash_file, hash_source_set, stable_hash
from .logging import configure_logging, console, err_console, get_logger
from .units import (
    format_frequency,
    format_time,
    freq_mhz_to_period_ns,
    frequency_to_period,
    from_hz,
    from_seconds,
    nearly_equal,
    parse_frequency_string,
    parse_time_string,
    period_ns_to_freq_mhz,
    period_to_frequency,
    to_hz,
    to_seconds,
)

__all__ = [
    # enums are exported via *
    "hash_file",
    "hash_source_set",
    "stable_hash",
    "configure_logging",
    "console",
    "err_console",
    "get_logger",
    "to_seconds",
    "from_seconds",
    "to_hz",
    "from_hz",
    "period_to_frequency",
    "frequency_to_period",
    "period_ns_to_freq_mhz",
    "freq_mhz_to_period_ns",
    "format_time",
    "format_frequency",
    "parse_time_string",
    "parse_frequency_string",
    "nearly_equal",
]
