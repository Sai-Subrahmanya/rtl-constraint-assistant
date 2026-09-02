from .base import CommandRecord, ToolBackend, ToolInfo, blocked_result
from .common.mock import MockEDA
from .flow import run_flow
from .opensta.backend import OpenSTABackend, STAResult
from .yosys.backend import YosysBackend, SynthResult, _parse_yosys_stat


def get_tool(name: str):
    tools = {"yosys": YosysBackend, "opensta": OpenSTABackend, "mock": MockEDA}
    cls = tools.get(name.lower())
    if cls is None:
        raise ValueError(f"Unknown tool backend '{name}'. Available: {list(tools)}")
    return cls()


__all__ = [
    "ToolBackend", "ToolInfo", "CommandRecord", "blocked_result",
    "YosysBackend", "OpenSTABackend", "MockEDA", "SynthResult", "STAResult",
    "run_flow", "get_tool",
]
