"""
Top-level Design object — the normalized model populated by parsers
and consumed by all later analysis/inference stages (Manual §7.1).

Structural connectivity
-----------------------
The design stores raw directed dependency edges in ``comb_edges`` as they
are discovered by the parser adapter (continuous assigns, procedural
blocking/nonblocking assigns, conditional/mux/concatenation/part-select
dependencies, and cross-instance port connections).  Once parsing is
complete, call :func:`build_structural_connectivity` (invoked
automatically by :meth:`TimingGraph.build`) to construct the data-only
fanin/fanout graph, resolve hierarchy, and enumerate real timing paths.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from ..utils.enums import TimingPathClass
from .instance import Instance
from .module import Module, SourceLocation
from .net import CombEdge, HierPortConn, Net
from .port import Port
from .process import Process
from .register import Register

if TYPE_CHECKING:
    from .connectivity import StructuralGraph


class Design(BaseModel):
    """Normalised design representation."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    top_module: str | None = None
    modules: dict[str, Module] = Field(default_factory=dict)
    hierarchy: dict[str, list[str]] = Field(default_factory=dict)
    ports: dict[str, Port] = Field(default_factory=dict)
    nets: dict[str, Net] = Field(default_factory=dict)
    instances: dict[str, Instance] = Field(default_factory=dict)
    registers: dict[str, Register] = Field(default_factory=dict)
    processes: dict[str, Process] = Field(default_factory=dict)
    comb_edges: list[CombEdge] = Field(default_factory=list)
    hier_conns: list[HierPortConn] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    source_files: list[str] = Field(default_factory=list)
    source_locations: dict[str, SourceLocation] = Field(default_factory=dict)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)

    # Seed signals from the parser (naming/structural hints only — NOT
    # used to prove connectivity).
    clocks_seed: list[str] = Field(default_factory=list)
    resets_seed: list[str] = Field(default_factory=list)

    # Populated lazily by build_structural_connectivity()
    structural_paths: list[Any] = Field(default_factory=list)
    cdc_paths: list[Any] = Field(default_factory=list)
    _structural_graph_internal: "StructuralGraph | None" = None

    # ------------------------------------------------------------------
    # Index helpers
    # ------------------------------------------------------------------

    def top_module_obj(self) -> Module | None:
        return self.modules.get(self.top_module) if self.top_module else None

    def ports_of(self, module_name: str) -> list[Port]:
        return [p for p in self.ports.values() if p.parent_module == module_name]

    def top_ports(self) -> list[Port]:
        if not self.top_module:
            return []
        return [p for p in self.ports.values() if p.parent_module == self.top_module]

    def registers_of(self, module_name: str) -> list[Register]:
        return [r for r in self.registers.values() if r.parent_module == module_name]

    def top_registers(self) -> list[Register]:
        if not self.top_module:
            return []
        return self.registers_of(self.top_module)

    def instances_of(self, module_name: str) -> list[Instance]:
        return [i for i in self.instances.values() if i.parent_module == module_name]

    def processes_of(self, module_name: str) -> list[Process]:
        return [p for p in self.processes.values() if p.parent_module == module_name]

    def nets_of(self, module_name: str) -> list[Net]:
        return [n for n in self.nets.values() if n.parent_module == module_name]

    # ------------------------------------------------------------------
    # Combinational graph (raw; includes non-data control edges)
    # ------------------------------------------------------------------

    def raw_comb_graph(self) -> dict[str, set[str]]:
        g: dict[str, set[str]] = defaultdict(set)
        for e in self.comb_edges:
            g[e.src].add(e.dst)
        return g

    def comb_graph(self) -> dict[str, set[str]]:
        """Data-only combinational fanout graph.  Requires connectivity
        to have been built; falls back to raw graph if not."""
        if self._structural_graph_internal is not None:
            return dict(self._structural_graph_internal.data_fanout)
        return self.raw_comb_graph()

    def fanout(self, signal: str) -> set[str]:
        return self.comb_graph().get(signal, set())

    def fanin(self, signal: str) -> set[str]:
        if self._structural_graph_internal is not None:
            return self._structural_graph_internal.data_fanin.get(signal, set())
        g = defaultdict(set)
        for e in self.comb_edges:
            g[e.dst].add(e.src)
        return g.get(signal, set())

    def structural_graph(self) -> "StructuralGraph | None":
        return self._structural_graph_internal

    # ------------------------------------------------------------------
    # Connectivity build entry point
    # ------------------------------------------------------------------

    def build_connectivity(self) -> "StructuralGraph":
        """Build structural connectivity if not already done.  Safe to
        call multiple times (idempotent)."""
        if self._structural_graph_internal is not None:
            return self._structural_graph_internal
        # Local import to avoid cycle at module load time.
        from .connectivity import build_structural_connectivity
        g = build_structural_connectivity(self)
        self.structural_paths = list(g.paths)
        self.cdc_paths = list(g.cdc_paths)
        return g

    # ------------------------------------------------------------------
    # Path classification (structural, pre-timing) — now driven by the
    # real structural graph rather than count heuristics (Manual §J).
    # ------------------------------------------------------------------

    def classify_paths_structural(self) -> dict[TimingPathClass, int]:
        """Structural count of timing-path categories derived from the
        *real* connectivity graph (``structural_paths`` + ``cdc_paths``).

        The previous heuristic formulas (``len(inputs)*len(outputs)``,
        ``N*(N-1)`` for same-clock registers, etc.) have been removed:
        counts are now exactly the number of structurally enumerated
        paths.
        """
        counts: dict[TimingPathClass, int] = {c: 0 for c in TimingPathClass}
        # Ensure connectivity has been built.
        if self._structural_graph_internal is None:
            try:
                self.build_connectivity()
            except Exception:  # pragma: no cover — defensive
                return counts
        for p in self.structural_paths:
            counts[p.path_class] = counts.get(p.path_class, 0) + 1
        for p in self.cdc_paths:
            counts[TimingPathClass.CDC] = counts.get(TimingPathClass.CDC, 0) + 1
        return counts

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        d = self.model_dump(exclude={"_structural_graph_internal"})
        return d

    def write_snapshot(self, path: str | Path) -> None:
        import json
        Path(path).write_text(
            json.dumps(self.snapshot(), indent=2, default=str),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Human summary
    # ------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "top": self.top_module,
            "modules": len(self.modules),
            "ports": len(self.ports),
            "nets": len(self.nets),
            "instances": len(self.instances),
            "registers": len(self.registers),
            "processes": len(self.processes),
            "comb_edges": len(self.comb_edges),
            "structural_paths": len(self.structural_paths) + len(self.cdc_paths),
            "cdc_paths": len(self.cdc_paths),
            "hier_conns": len(self.hier_conns),
            "source_files": len(self.source_files),
            "clock_candidates": sorted(set(self.clocks_seed)),
            "reset_candidates": sorted(set(self.resets_seed)),
        }
