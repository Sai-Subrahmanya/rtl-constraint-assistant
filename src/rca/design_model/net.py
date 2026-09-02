"""
Net, signal, and structural-dependency representations.

CombEdge models one directed dependency in the *structural* graph:
from source signal ``src`` to destination signal ``dst``, with a
``DependencyKind`` distinguishing data, clock, reset, enable, assignment,
conditional, concatenation, part-select, and hierarchy-port-connection
edges.  No physical timing is inferred here — edges represent logical
signal connectivity only (Manual §16, §17, Step 1 requirement A).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..utils.enums import DependencyKind
from .module import SourceLocation


class Net(BaseModel):
    """A declared net / variable in a module."""
    hierarchical_name: str
    local_name: str
    parent_module: str
    width: int = 1
    net_kind: str = "wire"
    datatype: str = "logic"
    signed: bool = False
    is_input: bool = False
    is_output: bool = False
    source_location: SourceLocation | None = None


class CombEdge(BaseModel):
    """A directed edge in the structural dependency graph.

    The edge represents the fact that ``src`` appears in the expression
    (or control condition) that defines ``dst``.  No gate-level timing is
    implied.
    """
    src: str                       # hierarchical source signal name
    dst: str                       # hierarchical destination signal name
    kind: DependencyKind = DependencyKind.DATA
    via: str | None = None         # owning process/assignment id (if known)
    source_location: SourceLocation | None = None
    # Optional extra evidence: RHS operator context ("&", "?", "+", "{}", "[ ]", …)
    context: str | None = None

    def signature(self) -> tuple[str, str, str, str | None]:
        """Tuple identity used for de-duplication."""
        return (self.src, self.dst, self.kind.value, self.via)


class HierPortConn(BaseModel):
    """A resolved cross-instance port connection (Manual requirement G).

    Records that inside ``instance_hier`` the formal port ``port_name`` is
    connected to the parent-level signal ``actual_signal`` (hierarchical
    name).  Cross-module timing edges are *projected* through these
    connections when both sides are resolvable; otherwise they are left
    as UNKNOWN.
    """
    instance_hier: str
    module_name: str
    port_name: str
    direction: str                # "input" | "output" | "inout"
    actual_signal: str | None = None  # hierarchical name in parent scope
    source_location: SourceLocation | None = None
