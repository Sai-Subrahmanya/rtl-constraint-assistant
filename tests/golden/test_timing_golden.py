"""Golden timing-model tests.

Each test loads a small SystemVerilog file from the timing_corpus/
directory, builds a TimingGraph, and compares a *normalized* view of
the result against a hand-authored expected dictionary.  Normalization
is semantic (sets, sorted lists, etc.) so the tests are not sensitive
to accidental ordering differences.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from rca.parser.slang_adapter import SlangAdapter
from rca.timing_model import TimingGraph
from rca.utils.enums import ClockDomainRelationship, TimingPathClass

CORPUS = Path(__file__).parent / "timing_corpus"


def _build(sv_file: Path, top: str, user_clocks=None, user_rels=None):
    d = SlangAdapter().parse([str(sv_file)], top=top)
    return TimingGraph.build(d, user_clocks=user_clocks, user_relationships=user_rels)


def _normalize(tg: TimingGraph) -> dict:
    clks: dict[str, dict] = {}
    for nm, c in tg.clocks.items():
        clks[nm] = {
            "confidence": c.confidence,
            "edge": c.edge.value,
            "evidence_kinds": sorted({e.kind.value for e in c.evidence}),
            "is_generated": c.is_generated,
            "is_gated": c.is_gated,
            "is_mux": c.is_mux,
            "n_regs": len(c.registers_driven),
            "n_procs": len(c.processes),
        }
    rsts: dict[str, dict] = {}
    for nm, r in tg.resets.items():
        rsts[nm] = {
            "type": r.reset_type.value,
            "polarity": r.polarity.value,
            "evidence_kinds": sorted({e.kind.value for e in r.evidence}),
            "confidence": r.confidence,
            "n_regs": len(r.registers_driven),
        }
    doms = {d.id: {
        "name": d.name,
        "n_regs": len(d.register_paths),
        "n_resets": len(d.reset_ids),
    } for d in tg.domains.values()}
    edges = []
    for e in tg.domains_edges if hasattr(tg, "domains_edges") else tg.domain_edges:
        edges.append({
            "pair": tuple(sorted([e.clock_a, e.clock_b])),
            "relationship": e.relationship.value,
            "confidence": e.confidence,
            "cdc_paths": e.cdc_paths_observed,
        })
    cdc_count = sum(1 for p in tg.paths if p.path_type == TimingPathClass.CDC)
    missing = sorted({m["category"] for m in tg.missing_information()})
    gated = [g["output"] for g in tg.clock_gating_candidates]
    muxes = [m["output"] for m in tg.clock_mux_candidates]
    gen = [g["output"] for g in tg.generated_clock_candidates]
    return {
        "clocks": clks,
        "resets": rsts,
        "domains": doms,
        "edges": sorted(edges, key=lambda e: e["pair"]),
        "cdc_count": cdc_count,
        "missing_categories": missing,
        "gated_outputs": sorted(gated),
        "mux_outputs": sorted(muxes),
        "generated_outputs": sorted(gen),
    }


# ---------------------------------------------------------------------
# 01: dff_async_rstn — single posedge clk + async active-low rst_n
# ---------------------------------------------------------------------


def test_01_dff_async_rstn():
    t = _build(CORPUS / "01_dff_async_rstn.sv", top="dff_async_rstn")
    n = _normalize(t)
    assert set(n["clocks"].keys()) == {"clk"}
    assert n["clocks"]["clk"]["confidence"] == "HIGH"
    assert n["clocks"]["clk"]["edge"] == "posedge"
    assert n["clocks"]["clk"]["n_regs"] == 1
    assert "drives_register" in n["clocks"]["clk"]["evidence_kinds"]
    assert "edge_sensitive" in n["clocks"]["clk"]["evidence_kinds"]
    assert set(n["resets"].keys()) == {"rst_n"}
    assert n["resets"]["rst_n"]["type"] == "asynchronous"
    assert n["resets"]["rst_n"]["polarity"] == "active_low"
    assert n["resets"]["rst_n"]["confidence"] == "HIGH"
    assert n["cdc_count"] == 0
    assert "clock_period" in n["missing_categories"]
    assert n["edges"] == []


# ---------------------------------------------------------------------
# 02: two_clocks_sync — user declares clocks synchronous.
# ---------------------------------------------------------------------


def test_02_two_clocks_sync_user_rel():
    t = _build(
        CORPUS / "02_two_clocks_sync.sv",
        top="two_clocks_sync",
        user_rels=[{"clocks": ["clk_a", "clk_b"],
                    "relationship": "synchronous", "fixed": True}],
    )
    n = _normalize(t)
    assert set(n["clocks"].keys()) == {"clk_a", "clk_b"}
    # Both clocks HIGH confidence.
    assert n["clocks"]["clk_a"]["confidence"] == "HIGH"
    assert n["clocks"]["clk_b"]["confidence"] == "HIGH"
    # Two domains (one per clock).
    assert len(n["domains"]) == 2
    # Single edge, marked SYNCHRONOUS HIGH by user.
    assert len(n["edges"]) == 1
    e = n["edges"][0]
    assert e["pair"] == ("clk_a", "clk_b")
    assert e["relationship"] == "synchronous"
    assert e["confidence"] == "HIGH"
    assert n["cdc_count"] == 0  # no structural crossing
    # No reset.
    assert n["resets"] == {}


def test_02_two_clocks_default_unknown():
    """Without a user declaration the relationship MUST be UNKNOWN."""
    t = _build(CORPUS / "02_two_clocks_sync.sv", top="two_clocks_sync")
    n = _normalize(t)
    assert len(n["edges"]) == 1
    assert n["edges"][0]["relationship"] == "unknown"
    assert "clock_relationship" in n["missing_categories"]


# ---------------------------------------------------------------------
# 03: sync_reset — reset not in sensitivity → not async.
# ---------------------------------------------------------------------


def test_03_sync_reset_not_async():
    t = _build(CORPUS / "03_sync_reset.sv", top="sync_reset")
    n = _normalize(t)
    assert "clk" in n["clocks"]
    if "rst" in n["resets"]:
        # If rst is detected, it MUST NOT be edge_sensitive/async.
        kinds = set(n["resets"]["rst"]["evidence_kinds"])
        assert "edge_sensitive" not in kinds
        assert n["resets"]["rst"]["type"] != "asynchronous"
    else:
        # Acceptable too: sync reset is conservative; absence is OK.
        pass


# ---------------------------------------------------------------------
# 04: cdc_sync2 — two-flop synchronizer → CDC path observed, but clocks
# default to UNKNOWN (not auto-marked asynchronous).
# ---------------------------------------------------------------------


def test_04_cdc_sync2_cdc_observed_but_unknown_relation():
    t = _build(CORPUS / "04_cdc_sync2.sv", top="cdc_sync2")
    n = _normalize(t)
    assert set(n["clocks"].keys()) == {"clk_a", "clk_b"}
    assert n["cdc_count"] >= 1
    assert len(n["edges"]) == 1
    e = n["edges"][0]
    assert e["pair"] == ("clk_a", "clk_b")
    assert e["relationship"] == "unknown"
    assert e["cdc_paths"] >= 1
    assert "clock_relationship" in n["missing_categories"]


# ---------------------------------------------------------------------
# 05: clk_name_as_data — "clk_sel" is plain data, not a clock.
# ---------------------------------------------------------------------


def test_05_clk_sel_is_data_not_clock():
    t = _build(CORPUS / "05_clk_name_as_data.sv", top="clk_name_as_data")
    n = _normalize(t)
    assert set(n["clocks"].keys()) == {"clk"}
    assert "clk_sel" not in n["clocks"]
    assert n["clocks"]["clk"]["n_regs"] == 1
    assert n["clocks"]["clk"]["confidence"] == "HIGH"
