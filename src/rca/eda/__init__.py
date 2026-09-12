from .base import CommandRecord, ToolBackend, ToolInfo, blocked_result
from .common.mock import MockEDA
from .flow import index_deferred_history_evidence, run_flow
from .opensta.backend import OpenSTABackend, STAResult
from .preflight import (
    CapabilityCheck,
    CapabilityClassification,
    CapabilityStatus,
    EDAPreflight,
    environment_fingerprint,
    preflight_symbiyosys,
    preflight_yosys_opensta,
)
from .yosys.backend import SynthResult, YosysBackend, _parse_yosys_stat


def get_tool(name: str):
    tools = {"yosys": YosysBackend, "opensta": OpenSTABackend, "mock": MockEDA}
    cls = tools.get(name.lower())
    if cls is None:
        raise ValueError(f"Unknown tool backend '{name}'. Available: {list(tools)}")
    return cls()


__all__ = [
    "CapabilityCheck",
    "CapabilityClassification",
    "CapabilityStatus",
    "CommandRecord",
    "EDAPreflight",
    "MockEDA",
    "OpenSTABackend",
    "STAResult",
    "SynthResult",
    "ToolBackend",
    "ToolInfo",
    "YosysBackend",
    "_parse_yosys_stat",
    "blocked_result",
    "environment_fingerprint",
    "get_tool",
    "index_deferred_history_evidence",
    "preflight_symbiyosys",
    "preflight_yosys_opensta",
    "run_flow",
]
