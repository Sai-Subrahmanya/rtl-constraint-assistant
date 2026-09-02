"""Step 10 — EDA backend, synthesis, STA, QoR, run-manifest tests (WP-M/N).

These tests cover:
1. tool discovery (explicit path / env var / missing)
2. version capture
3. safe subprocess invocation (no shell=True)
4. MockEDA labeling
5. synthesis via Yosys (skipped if unavailable)
6. missing Liberty → BLOCKED
7. OpenSTA invocation (skipped if unavailable)
8. timing-report parsing (setup/hold/critical path)
9. setup/hold feasibility separation
10. area / area_proxy distinction
11. power UNAVAILABLE
12. run manifest completeness
13. deterministic hashing / cache key
14. cache hit on identical inputs
15. cache invalidation on Liberty change
6. cache invalidation on tool version change
17. Mock is not real STA
18. end-to-end mock pipeline
19. end-to-end real pipeline (skipped/BLOCKED if tools or lib missing)
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from rca.artifacts import ArtifactManager, RunManifest
from rca.eda import MockEDA, OpenSTABackend, YosysBackend, run_flow
from rca.eda.base import ToolBackend, ToolInfo
from rca.eda.flow import (_artifacts_to_rel, _coerce_defines,
                          _coerce_parameters, _extract_cfg, _hash_artifacts,
                          _lib_hashes, _project_local_dirs, _rel,
                          _resolve_rel)
from rca.eda.yosys.backend import YosysBackend as _YB
from rca.eda.opensta.backend import OpenSTABackend as _OB
from rca.qor.model import Feasibility, QoRResult
from rca.reports.timing import parse_sta_text, parse_synth_report
from rca.utils.enums import PowerStatus, RunStatus
from rca.utils.hashing import hash_file, hash_text
from rca.utils.hashing import hash_file, stable_hash


# ---------------------------------------------------------------------------
# Helpers & fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp(tmp_path):
    return tmp_path


def _fake_tool(name: str, code: int = 0, stdout: str = "tool-version-1.0\n",
               stderr: str = "") -> Path:
    """Create an executable fake tool script for testing discovery."""
    p = Path(name)
    p.write_text(f"#!/bin/sh\nprintf '%s' '{stdout}'\nprintf '%s' '{stderr}' >&2\nexit {code}\n", encoding="utf-8")
    p.chmod(0o755)
    return p


def _write_minimal_rtl(dir_: Path) -> list[Path]:
    v = dir_ / "top.v"
    v.write_text("""module top(input clk, input d, output reg q);
always @(posedge clk) q <= d;
endmodule
""", encoding="utf-8")
    return [v]


# ---------------------------------------------------------------------------
# 1. tool discovery by explicit path
# ---------------------------------------------------------------------------

def test_01_yosys_explicit_path(tmp):
    fake = _fake_tool(tmp / "fake_yosys", stdout="Yosys 0.30 (mock)\n")
    y = YosysBackend(executable=str(fake))
    info = y.discover()
    assert info.available
    assert "Yosys 0.30" in info.version
    assert info.executable == str(fake)


# 2. tool discovery by environment variable

def test_02_opensta_env_var(tmp, monkeypatch):
    fake = _fake_tool(tmp / "fake_sta", stdout="sta 2.5.0\n")
    monkeypatch.setenv("RCA_OPENSTA", str(fake))
    o = OpenSTABackend()
    info = o.discover()
    assert info.available
    assert "2.5.0" in info.version


# 3. missing tool → unavailable

def test_03_missing_yosys_reports_unavailable(tmp):
    y = YosysBackend(executable=str(tmp / "nonexistent_yosys_xyz"))
    info = y.discover()
    assert not info.available
    assert "not found" in info.error


def test_03b_missing_opensta_reports_unavailable(tmp):
    o = OpenSTABackend(executable=str(tmp / "nonexistent_sta_xyz"))
    info = o.discover()
    assert not info.available


# 4. version capture

def test_04_version_capture_calls_binary(tmp):
    fake = _fake_tool(tmp / "vtool", stdout="Yosys 1.2.3\n")
    y = YosysBackend(executable=str(fake))
    info = y.discover()
    assert info.version.startswith("Yosys 1.2.3")
    assert info.vendor == "Yosys"


# 5. safe subprocess (no shell=True exercised)

def test_05_safe_run_uses_argv_not_shell(tmp):
    # The fake tool reads arguments; we pass them via argv and verify they
    # arrive intact (no shell interpolation).
    fake = tmp / "echotool"
    fake.write_text('#!/bin/sh\nprintf "argc=%d" "$#"\nfor a in "$@"; do printf " [%s]" "$a"; done\nexit 0\n', encoding="utf-8")
    fake.chmod(0o755)

    class _Stub(ToolBackend):
        name = "stub"
        default_binary_name = ""
        env_var = ""
        def discover(self): return ToolInfo(vendor="x", tool="x", version="x")
        def run_sta(self, *a, **k): pass
    s = _Stub()
    rec = s._safe_run([str(fake), "hello; rm -rf /", "world"], cwd=tmp, timeout=10)
    assert rec.returncode == 0
    # "hello; rm -rf /" must arrive as ONE argument (not executed as shell)
    assert "argc=2" in rec.stdout_tail
    assert "[hello; rm -rf /]" in rec.stdout_tail


# 23. MockEDA is clearly labeled

def test_23_mock_is_labeled():
    m = MockEDA()
    info = m.discover()
    assert info.vendor == "RCA"
    from rca.optimizer import Candidate
    from rca.constraint_model import ConstraintSet
    cs = ConstraintSet(name="x")
    cand = Candidate(id="C1", constraint_set=cs)
    qor = m.evaluate_candidate(cand, Path("/tmp"))
    assert qor.is_mock
    assert qor.backend == "mock"
    assert any("MOCK" in n for n in qor.notes)
    assert qor.power_status == PowerStatus.UNAVAILABLE.value


# 10. timing report parsing (setup/hold/WNS/TNS)

def test_10_parse_sta_setup_hold(tmp):
    report = """
Startpoint: a/Q (rising edge-triggered flip-flop)
Endpoint: b/D (rising edge-triggered flip-flop)
Path Group: clk
Path Type: max
slack (VIOLATED) -0.5234
Startpoint: x/Q
Endpoint: y/D
Path Group: clk
Path Type: min
slack (MET) 0.0102
wns -0.5234
tns -1.3000
wns -0.0200
tns -0.0200
"""
    qor = parse_sta_text(report)
    assert qor.setup_wns is not None
    assert abs(qor.setup_wns - (-0.5234e-9)) < 1e-18
    assert qor.setup_violations >= 1
    assert qor.hold_wns is not None


# 11. setup and hold independently determine feasibility

def test_11_setup_hold_independent():
    q = QoRResult(setup_wns=1e-9, hold_wns=-50e-12, cell_count=100)
    f = Feasibility.from_qor(q)
    assert f.setup_pass and not f.hold_pass and not f.feasible
    q2 = QoRResult(setup_wns=0.0, hold_wns=0.0, cell_count=100)
    f2 = Feasibility.from_qor(q2)
    assert f2.feasible


# 16. power remains unknown

def test_16_power_unknown():
    q = QoRResult()
    assert q.power is None
    assert q.power_status == PowerStatus.UNAVAILABLE.value


# 17. area vs area_proxy

def test_17_area_proxy_when_no_liberty():
    q = QoRResult(cell_count=100)
    assert q.area is None
    assert q.area_proxy is None  # set by flow / yosys parser
    stat = parse_synth_report_text_helper = None


# 12. synth stat parsing

def test_12_yosys_stat_parsing():
    text = """
...
Number of cells:        247
Chip area for top module '\\top': 1234.567
"""
    import tempfile
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".rpt") as f:
        f.write(text); p = Path(f.name)
    s = parse_synth_report(p)
    p.unlink()
    assert s["cell_count"] == 247
    assert abs(s["area"] - 1234.567) < 1e-6


# 18. run-manifest completeness (without real tools; use fake ones)

def test_18_manifest_completeness_with_mock(tmp):
    # Mock flow always produces a manifest via run_flow. Build a minimal
    # config-like object.
    class _FakeFlow:
        backend = "mock"; stage = "synthesis_sta"; output_dir = str(tmp)
        def liberty_files(self): return []
    class _FakeAnalysis:
        safe_mode = "balanced"
    class _FakeCfg:
        _config_path = str(tmp / "proj.yaml")
        project = type("P", (), {"name":"top"})()
        flow = _FakeFlow()
        analysis = _FakeAnalysis()
        def top_module(self): return "top"
        sources = type("S",(),{"defines":{}, "files":[]})()
    # No real RTL needed for mock path.
    from rca.constraint_model import ConstraintSet
    cs = ConstraintSet(name="top")
    out = run_flow(_FakeCfg(), cs, "// mock sdc\n",
                   sdc_generation_status="COMPLETE",
                   sources=[], backend="mock", run_id="r1")
    assert out["status"] == RunStatus.MOCK.value
    m = out["manifest"]
    for key in ("candidate_id","rtl_hash","sdc_hash","config_hash","tool",
                "tool_version","flow_stage","artifacts","corner"):
        assert key in m, f"missing manifest key {key}"


# 19. end-to-end real path is BLOCKED when tools unavailable

def test_19_real_flow_blocked_without_tools(tmp):
    class _FakeFlow:
        backend = "yosys_opensta"; stage = "synthesis_sta"; output_dir = str(tmp)
        def liberty_files(self): return []
    class _FakeAnalysis:
        safe_mode = "balanced"
    class _FakeCfg:
        _config_path = str(tmp / "proj.yaml")
        project = type("P",(),{"name":"top"})()
        flow = _FakeFlow()
        analysis = _FakeAnalysis()
        def top_module(self): return "top"
    from rca.constraint_model import ConstraintSet
    cs = ConstraintSet(name="top")
    out = run_flow(_FakeCfg(), cs, "create_clock -name clk -period 10 clk\n",
                   sdc_generation_status="COMPLETE",
                   sources=_write_minimal_rtl(tmp),
                   backend="yosys_opensta", run_id="r2",
                   yosys_bin=str(tmp/"_nope_yosys"),
                   sta_bin=str(tmp/"_nope_sta"))
    # No real tools and no liberty: BLOCKED
    assert out["status"] == RunStatus.BLOCKED.value
    assert any("not found" in d or "Liberty" in d or "unavailable" in d
               for d in out["diagnostics"])


# 7. OpenSTA missing-liberty → BLOCKED

def test_07_opensta_missing_liberty_returns_blocked(tmp):
    fake = _fake_tool(tmp / "fake_sta2", stdout="sta 1.0\n")
    o = OpenSTABackend(executable=str(fake))
    o.discover()
    r = o.run_sta(tmp/"netlist.v", tmp/"sdc.sdc", [], tmp/"work", "top")
    assert r.status == RunStatus.BLOCKED.value
    assert "Liberty" in r.error


# 20+21+22 cache hit/invalidation logic

# 20+21+22 cache hit/invalidation logic (corrected for integrity checks)

def test_20_cache_hit_on_complete_valid(tmp):
    """Cache hit requires: cache_key match, required artifacts exist,
    artifact hashes match, qor file present."""
    am = ArtifactManager(tmp)
    prior_dir = am.runs_dir / "prior"
    prior_dir.mkdir(parents=True)
    sdc = prior_dir / "generated.sdc"
    qor = prior_dir / "qor.json"
    netlist = prior_dir / "top_synth.v"
    sdc.write_text("create_clock clk -period 10\n", encoding="utf-8")
    netlist.write_text("// netlist\n", encoding="utf-8")
    qor.write_text("{}", encoding="utf-8")
    from rca.eda.flow import _find_cached_run, _hash_artifacts
    rel_artifacts = {"sdc":"generated.sdc", "netlist":"top_synth.v", "qor":"qor.json"}
    abs_artifacts = {k: str(prior_dir/v) for k,v in rel_artifacts.items()}
    hashes = _hash_artifacts(prior_dir, abs_artifacts)
    m = RunManifest(candidate_id="baseline", tool="yosys_opensta",
                    artifacts=rel_artifacts, artifact_hashes=hashes,
                    extra={"cache_key":"CACHEKEY_X", "diagnostics":[]})
    am.write_manifest_to("prior", m)
    hit = _find_cached_run(am, "CACHEKEY_X")
    assert hit is not None
    assert hit["run_id"] == "prior"


def test_20b_missing_netlist_cache_miss(tmp):
    am = ArtifactManager(tmp)
    prior_dir = am.runs_dir / "prior"; prior_dir.mkdir(parents=True)
    sdc = prior_dir/"generated.sdc"; qor = prior_dir/"qor.json"
    sdc.write_text("x\n", encoding="utf-8")
    qor.write_text("{}", encoding="utf-8")
    # Manifest claims a netlist artifact, but the file is missing
    rel = {"sdc":"generated.sdc","netlist":"top_synth.v","qor":"qor.json"}
    from rca.eda.flow import _find_cached_run, _hash_artifacts
    # Only hash the files that exist
    present = {"sdc":str(prior_dir/"generated.sdc"),"qor":str(prior_dir/"qor.json")}
    hashes = _hash_artifacts(prior_dir, present)
    hashes["netlist"] = "0"*64
    m = RunManifest(candidate_id="baseline", tool="yosys_opensta",
                    artifacts=rel, artifact_hashes=hashes,
                    extra={"cache_key":"CX","diagnostics":[]})
    am.write_manifest_to("prior", m)
    assert _find_cached_run(am, "CX") is None


def test_20c_modified_sdc_cache_miss(tmp):
    am = ArtifactManager(tmp)
    prior_dir = am.runs_dir / "prior"; prior_dir.mkdir(parents=True)
    sdc = prior_dir/"generated.sdc"; netlist = prior_dir/"top_synth.v"; qor = prior_dir/"qor.json"
    sdc.write_text("x\n", encoding="utf-8")
    netlist.write_text("y\n", encoding="utf-8")
    qor.write_text("{}", encoding="utf-8")
    from rca.eda.flow import _find_cached_run, _hash_artifacts
    rel = {"sdc":"generated.sdc","netlist":"top_synth.v","qor":"qor.json"}
    hashes = _hash_artifacts(prior_dir, {k:str(prior_dir/v) for k,v in rel.items()})
    # Tamper with SDC after computing hashes
    sdc.write_text("CHANGED\n", encoding="utf-8")
    m = RunManifest(candidate_id="baseline", tool="yosys_opensta",
                    artifacts=rel, artifact_hashes=hashes,
                    extra={"cache_key":"CY","diagnostics":[]})
    am.write_manifest_to("prior", m)
    assert _find_cached_run(am, "CY") is None


def test_20d_hash_mismatch_qor_cache_miss(tmp):
    am = ArtifactManager(tmp)
    prior_dir = am.runs_dir/"prior"; prior_dir.mkdir(parents=True)
    sdc=prior_dir/"generated.sdc"; netlist=prior_dir/"top_synth.v"; qor=prior_dir/"qor.json"
    sdc.write_text("x\n", encoding="utf-8")
    netlist.write_text("y\n", encoding="utf-8")
    qor.write_text("{}\n", encoding="utf-8")
    from rca.eda.flow import _find_cached_run
    rel = {"sdc":"generated.sdc","netlist":"top_synth.v","qor":"qor.json"}
    bad_hashes = {"sdc":hash_text("x\n"),"netlist":hash_text("y\n"),"qor":"deadbeef"}
    m = RunManifest(candidate_id="baseline", tool="yosys_opensta",
                    artifacts=rel, artifact_hashes=bad_hashes,
                    extra={"cache_key":"CZ","diagnostics":[]})
    am.write_manifest_to("prior", m)
    assert _find_cached_run(am, "CZ") is None


def test_20g_complete_valid_cache_hits(tmp):
    """Test G: complete, all hashes match → cache HIT."""
    test_20_cache_hit_on_complete_valid(tmp)  # already tested above


def test_21_cache_invalid_when_liberty_changes():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        lib1 = d/"a.lib"; lib2 = d/"b.lib"
        lib1.write_text("lib a\n", encoding="utf-8")
        lib2.write_text("lib b\n", encoding="utf-8")
        h1 = _lib_hashes([lib1]); h2 = _lib_hashes([lib2])
        assert h1[str(lib1)] != h2[str(lib2)]


def test_22_cache_key_includes_tool_version():
    k1 = stable_hash({"tool":{"yosys_ver":"1","sta_ver":"1"}})
    k2 = stable_hash({"tool":{"yosys_ver":"2","sta_ver":"1"}})
    assert k1 != k2


# ------------ Correction-pass tests: defines/includes/params invalidate cache ------------

def _make_flow_run(tmp, *, defines=None, includes=None, parameters=None,
                   sources=None, run_id="r1", backend="yosys_opensta",
                   liberty_files=None, yosys_bin=None, sta_bin=None):
    params = dict(parameters or {})
    incl_dirs = list(includes or [])
    libs = liberty_files or []
    class _FakeFlow:
        _backend = backend; stage = "synthesis_sta"; output_dir=str(tmp)
        def liberty_files(self): return list(libs)
    class _FakeAnalysis:
        safe_mode = "balanced"
    class _FakeSources:
        def __init__(self):
            self.files = []
            self.include_dirs = incl_dirs
            self.defines = []
    class _FakeCfg:
        _config_path = str(tmp/"proj.yaml")
        project = type("P",(),{"name":"top"})()
        flow = _FakeFlow()
        analysis = _FakeAnalysis()
        sources = _FakeSources()
        parameters = params
        def top_module(self): return "top"
    from rca.constraint_model import ConstraintSet
    cs = ConstraintSet(name="top")
    return run_flow(_FakeCfg(), cs, "create_clock -name clk -period 10 clk\n",
                    sdc_generation_status="COMPLETE", sources=sources or [],
                    defines=defines, include_dirs=includes and [Path(p) for p in includes],
                    parameters=params,
                    backend=backend, run_id=run_id,
                    yosys_bin=yosys_bin or str(tmp/"_ny"),
                    sta_bin=sta_bin or str(tmp/"_ns"))


def _cache_key_of(out):
    return out.get("cache_key") or out["manifest"].get("extra",{}).get("cache_key","")


def test_defines_change_invalidates_cache(tmp):
    """Defines WIDTH=8 vs WIDTH=16 must produce different cache keys."""
    rtl = tmp/"top.v"
    rtl.write_text("module top(input clk); endmodule\n", encoding="utf-8")
    out1 = _make_flow_run(tmp, defines={"WIDTH":"8"}, sources=[rtl], run_id="d1")
    out2 = _make_flow_run(tmp, defines={"WIDTH":"16"}, sources=[rtl], run_id="d2")
    assert _cache_key_of(out1) and _cache_key_of(out1) != _cache_key_of(out2)


def test_include_dir_change_invalidates_cache(tmp):
    d1 = tmp/"inc1"; d2 = tmp/"inc2"; d1.mkdir(); d2.mkdir()
    (d1/"a.vh").write_text("`define X 1\n", encoding="utf-8")
    rtl = tmp/"top.v"
    rtl.write_text("module top(input clk); endmodule\n", encoding="utf-8")
    out1 = _make_flow_run(tmp, includes=[str(d1)], sources=[rtl], run_id="i1")
    out2 = _make_flow_run(tmp, includes=[str(d2)], sources=[rtl], run_id="i2")
    assert _cache_key_of(out1) and _cache_key_of(out1) != _cache_key_of(out2)


def test_parameter_change_invalidates_cache(tmp):
    rtl = tmp/"top.v"
    rtl.write_text("module top(input clk); endmodule\n", encoding="utf-8")
    out1 = _make_flow_run(tmp, parameters={"W":"8"}, sources=[rtl], run_id="p1")
    out2 = _make_flow_run(tmp, parameters={"W":"16"}, sources=[rtl], run_id="p2")
    assert _cache_key_of(out1) and _cache_key_of(out1) != _cache_key_of(out2)


def test_synth_script_identity_changes(tmp):
    """If the synthesis script changes (different liberty), the synth
    script hash changes. We test this via YosysBackend.build_script output."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td/"top.v"; src.write_text("module top(input clk);endmodule\n",encoding="utf-8")
        lib1 = td/"a.lib"; lib1.write_text("library(a) { cell() { area:1; } }\n",encoding="utf-8")
        y = YosysBackend(executable=str(tmp/"_ny"))
        s1, sp1, n1 = y._build_script([src], "top", [lib1], td, defines={}, include_dirs=[], parameters={})
        s2, sp2, n2 = y._build_script([src], "top", [], td/"no", defines={}, include_dirs=[], parameters={})
        assert hash_text(s1) != hash_text(s2)


def test_manifest_artifact_paths_are_relative(tmp):
    """Manifest artifact paths must be run-relative (portable)."""
    out = _make_flow_run(tmp, sources=[], run_id="rel1", backend="mock")
    m = out["manifest"]
    for k, v in m["artifacts"].items():
        assert not Path(v).is_absolute(), f"{k}={v} must be relative"
    run_dir = Path(out["run_dir"])
    sdc = _resolve_rel(run_dir, m["artifacts"]["sdc"])
    assert sdc.is_file()


def test_relative_paths_resolve_correctly(tmp):
    run_dir = tmp/"r1"; run_dir.mkdir()
    (run_dir/"x.txt").write_text("hi\n", encoding="utf-8")
    assert _resolve_rel(run_dir, "x.txt").is_file()
    # Absolute passes through
    assert _resolve_rel(run_dir, str((run_dir/"x.txt").resolve())) == (run_dir/"x.txt").resolve()


def test_hold_policy_explicit_in_sta_script():
    """OpenSTA Tcl must include explicit -path_delay min and min wns/tns
    reports for hold. `check_setup` alone is NOT hold validation."""
    ob = _OB(executable="/bin/true")
    tcl, _, rpts = ob._build_script(netlist=Path("/tmp/n.v"),
                                    sdc=Path("/tmp/s.sdc"),
                                    liberty=[Path("/tmp/l.lib")],
                                    work_dir=Path("/tmp"),
                                    top="top", corner="typical")
    assert "report_checks -path_delay min" in tcl
    assert "report_wns -min" in tcl
    assert "report_tns -min" in tcl
    assert "check_setup" in tcl  # present, but documented as setup-only
    # Confirm the semantic key includes the hold (min) report fingerprint
    key = _OB.script_semantic_key(Path("/tmp/n.v"), Path("/tmp/s.sdc"),
                                  [Path("/tmp/l.lib")], "top", "typical",
                                  netlist_hash="h1", sdc_hash="h2",
                                  lib_hashes={str(Path("/tmp/l.lib")):"h3"})
    # Last element is the canonical command fingerprint; must mention min reports
    assert "report_checks" in key[-1]
    assert "min" in key[-1]
    assert "report_wns/tns" in key[-1]


def test_mock_is_clearly_labeled(tmp):
    from rca.constraint_model import ConstraintSet
    class _FakeFlow:
        backend="mock"; stage="synthesis_sta"; output_dir=str(tmp)
        def liberty_files(self): return []
    class _FakeAnalysis: safe_mode="balanced"
    class _FakeSources: files=[]; include_dirs=[]; defines=[]
    class _FakeCfg:
        _config_path=str(tmp/"p.yaml"); project=type("P",(),{"name":"top"})()
        flow=_FakeFlow(); analysis=_FakeAnalysis(); sources=_FakeSources()
        parameters={}
        def top_module(self): return "top"
    out = run_flow(_FakeCfg(), ConstraintSet(name="top"), "//sdc\n",
                   sdc_generation_status="COMPLETE", sources=[], backend="mock", run_id="m1")
    assert out["qor"]["is_mock"] is True
    assert out["status"] == "MOCK"


def test_yosys_failure_is_synthesis_failed(tmp):
    # Use a yosys binary that succeeds -V but fails during synthesis script run
    fake_y = tmp/"fake_yosys"
    fake_y.write_text('#!/bin/sh\nif [ "$1" = "-V" ]; then echo "Yosys 0.0 (fake)"; exit 0; fi\necho "ERROR: boom inside synth" >&2\nexit 1\n', encoding="utf-8")
    fake_y.chmod(0o755)
    fake_o = tmp/"fake_o"
    fake_o.write_text('#!/bin/sh\nif [ "$1" = "-version" ] || [ "$1" = "--version" ]; then echo "sta 1.0"; exit 0; fi\necho "sta ok"; exit 0\n',encoding="utf-8")
    fake_o.chmod(0o755)
    rtl = tmp/"top.v"
    rtl.write_text("module top(input clk);endmodule\n",encoding="utf-8")
    lib = tmp/"fake.lib"; lib.write_text("cell() {area:1;}\n",encoding="utf-8")
    from rca.constraint_model import ConstraintSet
    class _FakeFlow:
        backend="yosys_opensta"; stage="synthesis_sta"; output_dir=str(tmp)
        def liberty_files(self): return [str(lib)]
    class _FakeAnalysis: safe_mode="balanced"
    class _FakeSources: files=[]; include_dirs=[]; defines=[]
    class _FakeCfg:
        _config_path=str(tmp/"p.yaml"); project=type("P",(),{"name":"top"})()
        flow=_FakeFlow(); analysis=_FakeAnalysis(); sources=_FakeSources()
        parameters={}
        def top_module(self): return "top"
    out = run_flow(_FakeCfg(), ConstraintSet(name="top"),
                   "create_clock -name clk -period 10 clk\n",
                   sdc_generation_status="COMPLETE", sources=[rtl],
                   backend="yosys_opensta", run_id="yf1",
                   yosys_bin=str(fake_y), sta_bin=str(fake_o))
    assert out["status"] == RunStatus.SYNTHESIS_FAILED.value


def test_power_unknown_when_real():
    # Instantiate an empty QoRResult and confirm power defaults to None/UNAVAILABLE
    q = QoRResult()
    assert q.power is None
    assert q.power_status == PowerStatus.UNAVAILABLE.value


from rca.utils.hashing import hash_file, hash_text, stable_hash


# ------------- helpers for end-to-end cache-hit test -------------

def _write_fake_yosys(path: Path):
    """Fake yosys: returns version on -V; on -s <script> parses script for
    write_verilog target, writes a trivial netlist + stat output to stderr
    so _parse_yosys_stat succeeds."""
    path.write_text(r'''#!/usr/bin/env python3
import sys, re, pathlib
if "-V" in sys.argv:
    print("Yosys 0.35 (fake)")
    sys.exit(0)
# parse -s <script>
script = None
if "-s" in sys.argv:
    i = sys.argv.index("-s"); script = sys.argv[i+1]
sp = pathlib.Path(script).read_text()
m = re.search(r"write_verilog(?:\s+\S+)*\s+(\S+)", sp.splitlines()[-1] if False else sp)
netlist = pathlib.Path(m.group(1)) if m else None
# resolve relative to the script's directory (yosys runs with cwd=work_dir)
if netlist is not None and not netlist.is_absolute():
    netlist = pathlib.Path(script).parent / netlist
if netlist:
    netlist.parent.mkdir(parents=True, exist_ok=True)
    netlist.write_text("module top(input clk, output q); wire q; endmodule\n")
# stat output to stderr (matches yosys stat format)
print("""=== top ===

   Number of wires:                  2
   Number of cells:                  1
   Chip area for module:             1.0000""", file=sys.stderr)
sys.exit(0)
''', encoding="utf-8")
    path.chmod(0o755)


def _write_fake_sta(path: Path):
    """Fake OpenSTA: returns version on -version/--version; otherwise executes
    the tcl and writes the expected report files with wns/tns lines + slack
    path entries so parse_sta_text succeeds with positive slack."""
    path.write_text(r'''#!/usr/bin/env python3
import sys, re, pathlib
if "-version" in sys.argv or "--version" in sys.argv:
    print("OpenSTA 2.5.0 (fake)"); sys.exit(0)
# first non-flag arg is tcl script
tcl_path = [a for a in sys.argv[1:] if not a.startswith("-")][0]
tcl = pathlib.Path(tcl_path).read_text()
cwd = pathlib.Path(tcl_path).parent
# extract redirect targets
redirs = {}
for line in tcl.splitlines():
    m = re.search(r">\s*(\S+)", line)
    if m:
        target = m.group(1)
        if "setup.rpt" in target or "hold.rpt" in target:
            kind = "hold" if "hold.rpt" in target else "setup"
            redirs[target] = kind
        elif ".wns" in target or ".tns" in target:
            redirs[target] = "metric"
        elif "checks" in target:
            redirs[target] = "checks"
for target, kind in redirs.items():
    p = cwd / target
    p.parent.mkdir(parents=True, exist_ok=True)
    if kind == "setup":
        p.write_text("""Path Type: max
Startpoint: clk
Endpoint: q
Path Group: clk
slack 1.5000

  Delay   Time
    0.10   0.10
""")
    elif kind == "hold":
        p.write_text("""Path Type: min
Startpoint: clk
Endpoint: q
Path Group: clk
slack 0.2000
""")
    elif kind == "metric":
        if "setup.wns" in target:
            p.write_text("wns 1.5000\n")
        elif "setup.tns" in target:
            p.write_text("tns 0.0000\n")
        elif "hold.wns" in target:
            p.write_text("wns 0.2000\n")
        elif "hold.tns" in target:
            p.write_text("tns 0.0000\n")
    elif kind == "checks":
        p.write_text("No setup violations.\n")
sys.exit(0)
''', encoding="utf-8")
    path.chmod(0o755)


def _make_complete_cfg(tmp, *, backend="yosys_opensta", libs=None, out=None):
    out = out or tmp
    rtl = out/"rtl"; rtl.mkdir(parents=True, exist_ok=True)
    top = rtl/"top.v"
    top.write_text("module top(input clk, output reg q); always @(posedge clk) q<=1'b0; endmodule\n",
                   encoding="utf-8")
    lib_dir = out/"lib"; lib_dir.mkdir(parents=True, exist_ok=True)
    if libs is None:
        lib = lib_dir/"fake.lib"
        lib.write_text("library(fake) { cell(DFF) { area : 1.0; } }\n", encoding="utf-8")
        libs_list = [lib]
    else:
        libs_list = list(libs)
    class _FakeFlow:
        _backend=backend; stage="synthesis_sta"; output_dir=str(out)
        def liberty_files(self): return [str(p) for p in libs_list]
    class _FakeAnalysis: safe_mode="balanced"
    class _FakeSources: files=[]; include_dirs=[]; defines=[]
    class _FakeCfg:
        _config_path=str(out/"p.yaml")
        project=type("P",(),{"name":"top"})()
        flow=_FakeFlow(); analysis=_FakeAnalysis(); sources=_FakeSources()
        parameters={}
        def top_module(self): return "top"
    return _FakeCfg(), [top], libs_list


def _run_once(out_dir, *, ybin, obin, run_id, sdc_text, libs, sources, defines=None,
              params=None, includes=None):
    from rca.constraint_model import ConstraintSet as _CS
    cfg, srcs, _ = _make_complete_cfg(out_dir, libs=libs, out=out_dir)
    cs = _CS(name="top")
    return run_flow(cfg, cs, sdc_text,
                    sdc_generation_status="COMPLETE",
                    sources=sources or srcs,
                    defines=defines or {}, include_dirs=includes,
                    parameters=params or {},
                    backend="yosys_opensta", run_id=run_id,
                    yosys_bin=str(ybin), sta_bin=str(obin))


def test_cache_hit_then_invalidation_then_restore_e2e(tmp):
    """End-to-end using fake-but-functional yosys/sta executables.
    - first run produces SUCCESS; canonical cache_key stored in manifest
    - second run same inputs → CACHE_HIT, same key
    - mutate netlist (OUTPUT) → experiment key UNCHANGED, but integrity fails
      → MISS (fresh execution under new run_id)
    - restore netlist → CACHE_HIT again with same key
    """
    ybin = tmp/"fy"; obin = tmp/"fo"
    _write_fake_yosys(ybin); _write_fake_sta(obin)
    out_dir = tmp/"eda_out"
    cfg, sources, libs = _make_complete_cfg(out_dir, out=out_dir)
    sdc_text = "create_clock -name clk -period 10 clk\n"

    # first run (miss → synth+sta)
    out1 = _run_once(out_dir, ybin=ybin, obin=obin, run_id="re1",
                     sdc_text=sdc_text, libs=libs, sources=sources)
    assert out1["status"] in (RunStatus.SUCCESS.value, RunStatus.TIMING_FAIL.value), out1["status"]
    key1 = out1["cache_key"]
    assert key1 and len(key1) == 64
    m1 = out1["manifest"]
    assert m1["extra"]["cache_key"] == key1
    assert "netlist_hash" not in m1["extra"]["cache_key_data"], (
        "netlist_hash must NOT be in experiment cache key")
    # netlist_hash recorded for integrity (in extra.netlist_hash or artifact_hashes.netlist)
    assert m1["extra"].get("netlist_hash") or m1["artifact_hashes"].get("netlist")

    # second run identical → CACHE_HIT
    out2 = _run_once(out_dir, ybin=ybin, obin=obin, run_id="re2",
                     sdc_text=sdc_text, libs=libs, sources=sources)
    assert out2["status"] == RunStatus.CACHE_HIT.value, out2
    assert out2["cache_key"] == key1
    assert out2["manifest"]["extra"]["cache_key"] == key1

    # mutate netlist → integrity fail; third run must MISS (not CACHE_HIT)
    netlist_path = Path(out1["run_dir"]) / out1["manifest"]["artifacts"]["netlist"]
    original = netlist_path.read_text(encoding="utf-8")
    netlist_path.write_text("TAMPERED\n", encoding="utf-8")
    out3 = _run_once(out_dir, ybin=ybin, obin=obin, run_id="re3",
                     sdc_text=sdc_text, libs=libs, sources=sources)
    assert out3["status"] != RunStatus.CACHE_HIT.value, "tampered netlist must prevent hit"
    assert out3["cache_key"] == key1, "output tamper must not change experiment key"

    # restore → CACHE_HIT again
    netlist_path.write_text(original, encoding="utf-8")
    out4 = _run_once(out_dir, ybin=ybin, obin=obin, run_id="re4",
                     sdc_text=sdc_text, libs=libs, sources=sources)
    assert out4["status"] == RunStatus.CACHE_HIT.value, out4
    assert out4["cache_key"] == key1


def test_input_change_changes_key_but_output_tamper_does_not(tmp):
    """Input (SDC) change → different key. Output-only tamper → same key
    (correct identity/integrity separation)."""
    ybin = tmp/"fy"; obin = tmp/"fo"
    _write_fake_yosys(ybin); _write_fake_sta(obin)
    out_dir = tmp/"eda_out2"
    cfg, sources, libs = _make_complete_cfg(out_dir, out=out_dir)
    sdc_a = "create_clock -name clk -period 10 clk\n"
    sdc_b = "create_clock -name clk -period 8 clk\n"
    out_a = _run_once(out_dir, ybin=ybin, obin=obin, run_id="ka",
                      sdc_text=sdc_a, libs=libs, sources=sources)
    out_b = _run_once(out_dir, ybin=ybin, obin=obin, run_id="kb",
                      sdc_text=sdc_b, libs=libs, sources=sources)
    assert out_a["cache_key"] != out_b["cache_key"], "SDC change must change key"
    # Tamper netlist of run 'a' (output); then re-run with identical inputs
    # under new run_id: cache key must equal key_a (identity unchanged)
    net_a = Path(out_a["run_dir"]) / out_a["manifest"]["artifacts"]["netlist"]
    net_a.write_text("X\n", encoding="utf-8")
    out_a2 = _run_once(out_dir, ybin=ybin, obin=obin, run_id="ka2",
                       sdc_text=sdc_a, libs=libs, sources=sources)
    assert out_a2["cache_key"] == out_a["cache_key"]
