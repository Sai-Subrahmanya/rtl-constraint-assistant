"""Cross-process determinism tests for inference + canonical UCM snapshot.

These tests run the same design/config in two separate Python
subprocesses and assert that evidence IDs, constraint semantic keys,
and canonical JSON are byte-identical.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap


WORKER = textwrap.dedent(r"""
import sys, os, json, tempfile
sys.path.insert(0, sys.argv[1])
from rca.parser.slang_adapter import SlangAdapter
from rca.timing_model import TimingGraph
from rca.inference import InferenceEngine
from rca.config.model import ProjectConfig, ProjectInfo, UserClockSpec, IOPortSpec, ClockRelationshipSpec
from rca.constraint_model import ConstraintSet
from rca.provenance import AssumptionLedger
sv = '''module m(input clk, rst_n, d_in, clk_b, output reg q_a, q_b);
always_ff @(posedge clk or negedge rst_n) if (!rst_n) q_a<=1'b0; else q_a<=d_in;
always_ff @(posedge clk_b) q_b<=d_in;
endmodule'''
import tempfile, os
with tempfile.NamedTemporaryFile('w', suffix='.sv', delete=False) as f:
    f.write(sv); p = f.name
try:
    d = SlangAdapter().parse([p], top='m')
finally:
    os.unlink(p)
cfg = ProjectConfig(project=ProjectInfo(name='m', top='m', rtl_files=[p]))
cfg.constraints.user.clocks.append(UserClockSpec(name='clk', period='10ns', fixed=True))
cfg.constraints.user.io.inputs['d_in'] = IOPortSpec(delay='2ns')
ucs = []
for uc in cfg.constraints.user.clocks:
    info = {'name': uc.name, 'fixed': uc.fixed, 'port': uc.port,
            'period_seconds': uc.period_seconds()}
    ucs.append(info)
tg = TimingGraph.build(d, user_clocks=ucs)
cs = ConstraintSet(name='m')
ledg = AssumptionLedger()
eng = InferenceEngine()
FIXED_TS = "2025-01-01T00:00:00+00:00"
# Freeze run_id for byte-exact canonical snapshot comparison across processes.
cs.run_id = "deterministic-run"
report = eng.run(d, tg, cfg, cs, ledg, run_ts=FIXED_TS)
# Serialize what we care about:
evidence_ids = sorted({e.id for c in cs for e in c.provenance.evidence})
constraint_semkeys = sorted(c.semantic_key() for c in cs)
canonical_json = cs.to_canonical_json(indent=None)
missing = sorted((m['id'], m['category'], m['object']) for m in report.missing_information)
out = {
    'evidence_ids': evidence_ids,
    'constraint_semkeys': constraint_semkeys,
    'canonical_json': canonical_json,
    'missing': missing,
    'n_constraints': len(cs),
}
sys.stdout.write(json.dumps(out))
""")


def _run_in_subprocess():
    src_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(WORKER)
        script = f.name
    try:
        proc = subprocess.run(
            [sys.executable, script, src_dir],
            capture_output=True, text=True, check=True, timeout=60,
        )
        return json.loads(proc.stdout)
    finally:
        os.unlink(script)


def test_cross_process_evidence_ids_and_canonical_json_identical():
    a = _run_in_subprocess()
    b = _run_in_subprocess()
    # Evidence IDs
    assert a["evidence_ids"] == b["evidence_ids"], \
        f"evidence ids differ between processes\na={a['evidence_ids']}\nb={b['evidence_ids']}"
    # All evidence IDs start with "ev_" and have no Python hash artifacts.
    for eid in a["evidence_ids"]:
        assert eid.startswith("ev_"), f"evidence id not using stable prefix: {eid}"
        assert len(eid) == 3 + 12, f"evidence id unexpected length: {eid}"
        # The suffix after "ev_" must be pure hex (no process-unique hash leaks).
        assert all(c in "0123456789abcdef" for c in eid[3:]), \
            f"evidence id suffix not hex: {eid}"
    # Constraint semantic keys
    assert a["constraint_semkeys"] == b["constraint_semkeys"]
    # Canonical JSON byte-identical
    assert a["canonical_json"] == b["canonical_json"], "canonical JSON differs between processes"
    # Missing info
    assert a["missing"] == b["missing"]
    assert a["n_constraints"] == b["n_constraints"]


def test_same_evidence_same_id_in_process():
    """Quick in-process sanity: two semantically identical evidences get the same id."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
    from rca.inference._evidence import evidence_id, make_evidence
    e1 = make_evidence("CLK-001", "structural", "Clock 'clk' drives 1 register(s).",
                       source_objects=["clk"], created_at="2025-01-01T00:00:00+00:00")
    e2 = make_evidence("CLK-001", "structural", "Clock 'clk' drives 1 register(s).",
                       source_objects=["clk"], created_at="2026-01-01T00:00:00+00:00")
    assert e1.id == e2.id
    # different description
    e3 = make_evidence("CLK-001", "structural", "Clock 'clk' drives 2 register(s).",
                       source_objects=["clk"])
    assert e1.id != e3.id
    # source-object ordering must not matter
    e4 = make_evidence("X", "structural", "desc", source_objects=["a", "b"])
    e5 = make_evidence("X", "structural", "desc", source_objects=["b", "a"])
    assert e4.id == e5.id
    # different kind differs
    e6 = make_evidence("X", "user", "desc", source_objects=["a", "b"])
    assert e4.id != e6.id
    # Sanity: id is "ev_" + 12 hex chars (stable_hash truncated)
    assert e1.id.startswith("ev_")
    assert len(e1.id) == 3 + 12
    assert all(c in "0123456789abcdef" for c in e1.id[3:])


def test_evidence_dedup_uses_stable_ids():
    """Verifies the engine deduplicates across rules by stable evidence key."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
    sv = ("module m(input clk, d, output reg q); "
          "always_ff @(posedge clk) q<=d; endmodule")
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".sv", delete=False) as f:
        f.write(sv); p = f.name
    from rca.parser.slang_adapter import SlangAdapter
    from rca.timing_model import TimingGraph
    from rca.inference import InferenceEngine
    from rca.config.model import ProjectConfig, ProjectInfo, UserClockSpec
    from rca.constraint_model import ConstraintSet
    from rca.provenance import AssumptionLedger
    d = SlangAdapter().parse([p], top='m'); os.unlink(p)
    cfg = ProjectConfig(project=ProjectInfo(name='m', top='m', rtl_files=[p]))
    cfg.constraints.user.clocks.append(UserClockSpec(name='clk', period='10ns'))
    ucs = [{'name': 'clk', 'fixed': True, 'port': None, 'period_seconds': 10e-9}]
    tg = TimingGraph.build(d, user_clocks=ucs)
    cs = ConstraintSet(); ledg = AssumptionLedger(); eng = InferenceEngine()
    eng.run(d, tg, cfg, cs, ledg, run_ts="2025-01-01T00:00:00+00:00")
    clks = [c for c in cs if c.type.value == "create_clock"]
    assert len(clks) == 1
    # Evidence deduplication: multiple rules adding the same structural
    # evidence must collapse to one record (keyed by kind/desc/objs).
    ev_ids = [e.id for e in clks[0].provenance.evidence]
    assert len(ev_ids) == len(set(ev_ids)), f"duplicate evidence ids: {ev_ids}"
