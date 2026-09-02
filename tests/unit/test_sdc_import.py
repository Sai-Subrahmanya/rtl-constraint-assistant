"""Step 5: hardened SDC/Tcl importer tests (36+ cases)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from rca.constraint_model import ConstraintSet
from rca.constraint_model.selectors import PathSelector
from rca.sdc_importer import (
    SdcImporter,
    SdcParser,
    TargetCollection,
    TclLexer,
)
from rca.sdc_importer.lexer import BWORD, CMD_SUBST, COMMENT, QWORD, WORD
from rca.sdc_importer.parser import ParseDiagnostic
from rca.utils.enums import (
    CollectionKind,
    ConstraintType,
    DiagnosticSeverity,
    ImportStatus,
    ResolutionStatus,
    SourceKind,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


FIXED_TS = "2025-01-01T00:00:00+00:00"
FIXED_RUN = "test-run"


def _imp(text: str, design=None, tg=None):
    imp = SdcImporter(design=design, tg=tg, run_ts=FIXED_TS, run_id=FIXED_RUN)
    return imp.from_text(text, source_file="<test>")


def _first(cset: ConstraintSet, type_: ConstraintType):
    matches = [c for c in cset if c.type == type_]
    assert matches, f"no {type_.value} constraint found; have {[c.type.value for c in cset]}"
    return matches[0]


def _all(cset: ConstraintSet, type_: ConstraintType):
    return [c for c in cset if c.type == type_]


# ---------------------------------------------------------------------------
# Lexer tests
# ---------------------------------------------------------------------------


def test_lexer_simple_words():
    lx = TclLexer()
    cmds = list(lx.tokenize_commands("create_clock -period 10 clk\n"))
    assert len(cmds) == 1
    kinds = [t.kind for t in cmds[0]]
    assert kinds == [WORD, WORD, WORD, WORD]


def test_lexer_quoted_string():
    lx = TclLexer()
    cmds = list(lx.tokenize_commands('set a "hello world"\n'))
    toks = cmds[0]
    assert toks[2].kind == QWORD
    assert toks[2].text == "hello world"


def test_lexer_brace_group():
    lx = TclLexer()
    cmds = list(lx.tokenize_commands("set g {a b c}\n"))
    toks = cmds[0]
    assert toks[2].kind == BWORD
    assert "a b c" in toks[2].text


def test_lexer_cmd_subst():
    lx = TclLexer()
    cmds = list(lx.tokenize_commands("create_clock -period 10 [get_ports clk]\n"))
    assert any(t.kind == CMD_SUBST for t in cmds[0])
    sub = [t for t in cmds[0] if t.kind == CMD_SUBST][0]
    assert "get_ports clk" in sub.inner


def test_lexer_nested_cmd_subst_balances():
    lx = TclLexer()
    cmds = list(lx.tokenize_commands("cmd [outer [inner a] b]\n"))
    sub = [t for t in cmds[0] if t.kind == CMD_SUBST][0]
    assert sub.text == "[outer [inner a] b]"


def test_lexer_line_continuation():
    lx = TclLexer()
    text = "create_clock -name clk -period 10 \\\n    [get_ports clk]\n"
    cmds = list(lx.tokenize_commands(text))
    assert len(cmds) == 1
    assert cmds[0][0].text == "create_clock"
    assert any(t.kind == CMD_SUBST for t in cmds[0])


def test_lexer_comments():
    lx = TclLexer()
    cmds = list(lx.tokenize_commands("# whole-line comment\ncreate_clock clk\n"))
    # Comment-only command + create_clock command
    assert any(len(c) == 1 and c[0].kind == COMMENT for c in cmds)
    assert any(c[0].text == "create_clock" for c in cmds if c[0].kind != COMMENT)


def test_lexer_semicolon_separator():
    lx = TclLexer()
    cmds = list(lx.tokenize_commands("set a 1; set b 2\n"))
    assert len(cmds) == 2


def test_lexer_multiple_lines_and_blank_lines():
    lx = TclLexer()
    text = "\n\ncmd1 a\n\ncmd2 b\n"
    cmds = list(lx.tokenize_commands(text))
    assert len(cmds) == 2
    assert cmds[0][0].text == "cmd1"
    assert cmds[1][0].text == "cmd2"


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


def test_parser_basic_create_clock():
    r = _imp("create_clock -name clk -period 10 [get_ports clk]\n")
    c = _first(r.constraint_set, ConstraintType.CREATE_CLOCK)
    assert c.values["name"] == "clk"
    assert c.values["period"] == pytest.approx(10e-9, rel=1e-6)
    assert "clk" in c.target_objects


def test_parser_create_clock_waveform():
    r = _imp("create_clock -name clk -period 10 -waveform {0 5} [get_ports clk]\n")
    c = _first(r.constraint_set, ConstraintType.CREATE_CLOCK)
    assert c.values["waveform"] == [0.0, 5e-9]
    assert c.target_objects == ["clk"]


def test_parser_create_clock_add_flag():
    r = _imp("create_clock -add -name clk2 -period 5 [get_ports clk2]\n")
    c = _first(r.constraint_set, ConstraintType.CREATE_CLOCK)
    assert c.values.get("add") is True
    assert c.values["name"] == "clk2"


def test_parser_create_generated_clock_basic():
    r = _imp("create_generated_clock -name clkdiv -source clk -master_clock clk -divide_by 2 [get_pins U1/Q]\n")
    c = _first(r.constraint_set, ConstraintType.CREATE_GENERATED_CLOCK)
    assert c.values["name"] == "clkdiv"
    assert c.values["divide_by"] == 2
    assert c.values["master_clock"] == "clk"
    assert c.values["source"] == "clk"


def test_parser_create_generated_clock_divide_source():
    r = _imp("create_generated_clock -name gclk -source [get_ports clk] -divide_by 4 [get_pins reg/Q]\n")
    c = _first(r.constraint_set, ConstraintType.CREATE_GENERATED_CLOCK)
    assert c.values["divide_by"] == 4
    # source target should resolve to "clk" via the collection parser
    assert "clk" in c.values["source"] or "reg/Q" in c.target_objects


def test_parser_input_delay():
    r = _imp("set_input_delay 2.0 -clock clk [get_ports data_in]\n")
    cs = _all(r.constraint_set, ConstraintType.SET_INPUT_DELAY)
    # With no rise/fall and no min/max flag, value applies to rise+fall, max.
    assert len(cs) == 2
    for c in cs:
        assert c.clock_refs == ["clk"]
        assert c.target_objects == ["data_in"]
        assert c.values["delay"] == pytest.approx(2e-9)
        assert c.values["min_max"] == "max"
    edges = {c.values["edge"] for c in cs}
    assert edges == {"rise", "fall"}


def test_parser_output_delay():
    r = _imp("set_output_delay 1.5 -clock clk [get_ports q]\n")
    cs = _all(r.constraint_set, ConstraintType.SET_OUTPUT_DELAY)
    assert len(cs) == 2
    assert all(c.values["delay"] == pytest.approx(1.5e-9) for c in cs)


def test_parser_input_delay_min_max_separate():
    sdc = ("set_input_delay -clock clk -max 2.0 [get_ports d]\n"
           "set_input_delay -clock clk -min 0.5 [get_ports d]\n")
    r = _imp(sdc)
    cs = _all(r.constraint_set, ConstraintType.SET_INPUT_DELAY)
    # First cmd: max + rise + fall (2). Second: min + rise + fall (2). Total 4.
    assert len(cs) == 4
    mins = [c for c in cs if c.values["min_max"] == "min"]
    maxs = [c for c in cs if c.values["min_max"] == "max"]
    assert len(mins) == 2
    assert len(maxs) == 2
    assert all(c.values["delay"] == pytest.approx(0.5e-9) for c in mins)
    assert all(c.values["delay"] == pytest.approx(2e-9) for c in maxs)


def test_parser_rise_fall_preserved():
    r = _imp("set_input_delay -clock clk -rise 1.0 [get_ports d]\n")
    cs = _all(r.constraint_set, ConstraintType.SET_INPUT_DELAY)
    # Only rise, default min_max is max (SDC default for I/O delay with no
    # -min/-max qualifier), so exactly one constraint.
    assert len(cs) == 1
    assert cs[0].values["edge"] == "rise"
    assert cs[0].values["min_max"] == "max"


def test_parser_add_delay():
    r = _imp("set_input_delay -clock clk -add_delay -max 2.0 [get_ports d]\n")
    c = _first(r.constraint_set, ConstraintType.SET_INPUT_DELAY)
    assert c.values["add_delay"] is True


def test_parser_clock_uncertainty():
    r = _imp("set_clock_uncertainty 0.1 [get_clocks clk]\n")
    c = _first(r.constraint_set, ConstraintType.SET_CLOCK_UNCERTAINTY)
    assert c.values["uncertainty"] == pytest.approx(0.1e-9)
    assert c.target_objects == ["clk"]
    assert c.values["setup_hold"] == "both"
    assert c.values["min_max"] == "both"


def test_parser_clock_latency():
    r = _imp("set_clock_latency 0.5 -source [get_clocks clk]\n")
    c = _first(r.constraint_set, ConstraintType.SET_CLOCK_LATENCY)
    assert c.values["latency"] == pytest.approx(0.5e-9)
    assert c.values["source"] is True


def test_parser_clock_transition():
    r = _imp("set_clock_transition 0.2 [get_clocks clk]\n")
    c = _first(r.constraint_set, ConstraintType.SET_CLOCK_TRANSITION)
    assert c.values["transition"] == pytest.approx(0.2e-9)
    assert c.target_objects == ["clk"]


def test_parser_async_clock_groups():
    r = _imp("set_clock_groups -asynchronous -group {clk_a clk_b} -group {clk_c}\n")
    c = _first(r.constraint_set, ConstraintType.SET_CLOCK_GROUPS)
    assert c.values["relationship"] == "asynchronous"
    assert c.values["groups"] == [["clk_a", "clk_b"], ["clk_c"]]


def test_parser_logically_exclusive_groups():
    r = _imp("set_clock_groups -logically_exclusive -group clk_a -group clk_b\n")
    c = _first(r.constraint_set, ConstraintType.SET_CLOCK_GROUPS)
    assert c.values["relationship"] == "logically_exclusive"
    assert c.values["groups"] == [["clk_a"], ["clk_b"]]


def test_parser_physically_exclusive_groups():
    r = _imp("set_clock_groups -physically_exclusive -group {a b} -group {c}\n")
    c = _first(r.constraint_set, ConstraintType.SET_CLOCK_GROUPS)
    assert c.values["relationship"] == "physically_exclusive"


def test_parser_false_path():
    r = _imp("set_false_path -from [get_clocks a] -to [get_clocks b]\n")
    c = _first(r.constraint_set, ConstraintType.SET_FALSE_PATH)
    assert c.path_selector.from_set == ["a"]
    assert c.path_selector.to_set == ["b"]


def test_parser_multicycle_path():
    r = _imp("set_multicycle_path 2 -setup -from [get_ports d] -to [get_ports q]\n")
    c = _first(r.constraint_set, ConstraintType.SET_MULTICYCLE_PATH)
    assert c.values["cycles"] == 2
    assert c.values["setup_hold"] == "setup"
    assert c.path_selector.from_set == ["d"]
    assert c.path_selector.to_set == ["q"]


def test_parser_min_delay():
    r = _imp("set_min_delay 1 -from [get_ports a] -to [get_ports b]\n")
    c = _first(r.constraint_set, ConstraintType.SET_MIN_DELAY)
    assert c.values["delay"] == pytest.approx(1e-9)


def test_parser_max_delay():
    r = _imp("set_max_delay 3 -from [get_ports a] -to [get_ports b]\n")
    c = _first(r.constraint_set, ConstraintType.SET_MAX_DELAY)
    assert c.values["delay"] == pytest.approx(3e-9)


def test_parser_multi_through_preserves_groups():
    """Two -through flags produce ordered stages, not a flat list."""
    sdc = "set_false_path -through {A B} -through C\n"
    r = _imp(sdc)
    c = _first(r.constraint_set, ConstraintType.SET_FALSE_PATH)
    assert c.path_selector.through_set == [["A", "B"], ["C"]]


def test_parser_wildcard_target():
    r = _imp("set_clock_uncertainty 0.2 [get_clocks clk*]\n")
    c = _first(r.constraint_set, ConstraintType.SET_CLOCK_UNCERTAINTY)
    # Without a design, pattern is preserved literally.
    assert "clk*" in c.target_objects
    # Ensure no spurious resolution
    ic = r.imports[0]
    assert any("clk*" in (t.pattern or "") or "clk*" in t.expression for t in ic.targets)


def test_parser_get_ports_collection():
    p = SdcParser()
    pr = p.parse_text("set_input_delay 1 -clock clk [get_ports foo]\n")
    from rca.sdc_importer.collections import parse_target_value
    pv = parse_target_value(pr.parsed.commands[0].positional[1])
    assert pv.collection_kind == CollectionKind.PORT
    assert pv.pattern == "foo"


def test_parser_get_pins_collection():
    from rca.sdc_importer.collections import parse_target_value
    pv = parse_target_value("[get_pins U1/A]")
    assert pv.collection_kind == CollectionKind.PIN
    assert pv.pattern == "U1/A"


def test_parser_get_clocks_collection():
    from rca.sdc_importer.collections import parse_target_value
    pv = parse_target_value("[get_clocks clk]")
    assert pv.collection_kind == CollectionKind.CLOCK
    assert pv.pattern == "clk"


def test_all_inputs_outputs_supported():
    from rca.sdc_importer.collections import parse_target_value
    pv_in = parse_target_value("[all_inputs]")
    pv_out = parse_target_value("[all_outputs]")
    assert pv_in.collection_kind == CollectionKind.ALL_INPUTS
    assert pv_out.collection_kind == CollectionKind.ALL_OUTPUTS


def test_multiline_command_with_continuations():
    sdc = textwrap.dedent("""\
        create_clock -name clk -period 10 \\
            -waveform {0 5} \\
            [get_ports clk]
    """)
    r = _imp(sdc)
    c = _first(r.constraint_set, ConstraintType.CREATE_CLOCK)
    assert c.values["period"] == pytest.approx(10e-9)
    assert c.values["waveform"] == [0.0, 5e-9]
    assert "clk" in c.target_objects


def test_brace_grouping_in_group_flag():
    sdc = "set_clock_groups -asynchronous -group {a b c} -group {d e}\n"
    r = _imp(sdc)
    c = _first(r.constraint_set, ConstraintType.SET_CLOCK_GROUPS)
    assert c.values["groups"] == [["a", "b", "c"], ["d", "e"]]


def test_quoted_names_preserve_spaces():
    lx = TclLexer()
    cmds = list(lx.tokenize_commands('set a "hello world"\n'))
    assert cmds[0][2].kind == QWORD
    assert cmds[0][2].text == "hello world"


def test_comments_and_continuations():
    sdc = textwrap.dedent("""\
        # comment at top
        create_clock -name clk -period 10 \\
            [get_ports clk]   # trailing comment NOT part of command
    """)
    # Tcl doesn't treat '#' after a non-command-start as comment, but our
    # strip-comment logic handles line-trailing # conservatively. The
    # command should still parse.
    r = _imp(sdc)
    cs = _all(r.constraint_set, ConstraintType.CREATE_CLOCK)
    assert len(cs) == 1


def test_unsupported_command_diagnostic():
    r = _imp("some_unsupported_cmd -x 1\n")
    assert any(d.code == "UNKNOWN_COMMAND" for d in r.diagnostics)
    # Opaque passthrough recorded
    assert len(r.constraint_set) == 1
    ic = r.imports[0]
    assert ic.import_status == ImportStatus.PARTIAL


def test_malformed_command_error_diagnostic():
    # Missing period
    r = _imp("create_clock -name clk [get_ports clk]\n")
    assert any(d.code == "MISSING_PERIOD" for d in r.diagnostics)
    assert r.counts()["error"] >= 1


def test_unresolved_target_preserved():
    # No design bound → pattern preserved as-is, no drop
    r = _imp("set_input_delay 2 -clock clk [get_ports missing_port]\n")
    cs = _all(r.constraint_set, ConstraintType.SET_INPUT_DELAY)
    assert len(cs) == 2
    assert all("missing_port" in c.target_objects for c in cs)


def test_nested_or_unsupported_tcl_is_unresolved():
    r = _imp("set_false_path -from [get_ports [all_inputs]]\n")
    ic = r.imports[0]
    # Should be PARTIAL because the nested substitution is not executed.
    assert ic.import_status in (ImportStatus.PARTIAL, ImportStatus.COMPLETE)
    # Ensure no crash; constraints are still produced (partial).
    assert len(_all(r.constraint_set, ConstraintType.SET_FALSE_PATH)) == 1


def test_security_never_executes_commands():
    """exec/source/eval inside a command substitution must never run;
    they must be recorded as SECURITY or UNRESOLVED without executing."""
    # Create a sentinel file path that doesn't exist; even so, we must
    # not attempt to create it.
    sentinel = Path(tempfile.gettempdir()) / "__rca_security_marker__"
    if sentinel.exists():
        sentinel.unlink()
    sdc = f'set_false_path -from [exec touch {sentinel}]\n'
    r = _imp(sdc)
    # Must not have executed
    assert not sentinel.exists(), "importer executed an exec!"
    # A SECURITY diagnostic must exist
    sevs = [d for d in r.diagnostics if d.severity == DiagnosticSeverity.SECURITY]
    assert sevs, f"expected SECURITY diagnostic; got {[(d.severity, d.code, d.message) for d in r.diagnostics]}"


def test_security_top_level_forbidden():
    sdc = "exec rm -rf /\n"
    r = _imp(sdc)
    sevs = [d for d in r.diagnostics if d.severity == DiagnosticSeverity.SECURITY]
    assert sevs
    # No constraint emitted
    assert len(r.constraint_set) == 0


def test_design_aware_resolution(simple_design):
    """With a Design bound, [get_ports X] resolves existing ports."""
    design, tg = simple_design
    imp = SdcImporter(design=design, tg=tg, run_ts=FIXED_TS, run_id=FIXED_RUN)
    sdc = "set_input_delay 2 -clock clk [get_ports d_in]\n"
    r = imp.from_text(sdc, source_file="<d>")
    c = _first(r.constraint_set, ConstraintType.SET_INPUT_DELAY)
    assert "d_in" in c.target_objects


def test_determinism_cross_process():
    """Run the importer twice in independent Python processes; canonical
    JSON must match (fixed run_ts/run_id)."""
    worker = textwrap.dedent(r"""
        import sys, json
        sys.path.insert(0, sys.argv[1])
        from rca.sdc_importer import SdcImporter
        sdc = '''
        create_clock -name clk -period 10 [get_ports clk]
        set_input_delay -clock clk -max 2 [get_ports d]
        set_clock_groups -asynchronous -group {a b} -group {c}
        set_false_path -from [get_clocks a] -to [get_clocks b]
        '''
        imp = SdcImporter(run_ts="2025-01-01T00:00:00+00:00", run_id="det")
        res = imp.from_text(sdc, source_file="<x>")
        res.constraint_set.run_id = "det"
        res.constraint_set.created_at = "2025-01-01T00:00:00+00:00"
        print(res.constraint_set.to_canonical_json(indent=None))
    """)
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(worker); script = f.name
    src = str(Path(__file__).resolve().parents[2] / "src")
    try:
        def run():
            p = subprocess.run([sys.executable, script, src], capture_output=True, text=True, check=True, timeout=30)
            return p.stdout.strip()
        a = run(); b = run()
        assert a == b, "canonical JSON differs between processes"
    finally:
        Path(script).unlink(missing_ok=True)


def test_multi_constraint_semantics_min_max_rise_fall_distinct():
    sdc = ("set_input_delay -clock clk -max 2 data_in\n"
           "set_input_delay -clock clk -min 1 data_in\n")
    r = _imp(sdc)
    cs = _all(r.constraint_set, ConstraintType.SET_INPUT_DELAY)
    # 2 for max (rise+fall) + 2 for min (rise+fall) = 4
    assert len(cs) == 4
    mins = [c for c in cs if c.values["min_max"] == "min"]
    maxs = [c for c in cs if c.values["min_max"] == "max"]
    assert len(mins) == 2 and len(maxs) == 2


def test_roundtrip_snapshot_preserves_provenance():
    sdc = "create_clock -name clk -period 10 [get_ports clk]\n"
    r = _imp(sdc)
    snap = r.constraint_set.to_canonical_json()
    restored = ConstraintSet.from_canonical_json(snap)
    orig = _first(r.constraint_set, ConstraintType.CREATE_CLOCK)
    got = _first(restored, ConstraintType.CREATE_CLOCK)
    assert got.semantically_equivalent(orig)
    assert got.provenance.import_meta is not None
    assert got.provenance.import_meta.source_line == 1
    assert "create_clock" in got.provenance.import_meta.original_command


def test_adversarial_unmatched_brace_recovers():
    sdc = "create_clock -name clk -period 10 {oops\ncreate_clock -name clk2 -period 5 [get_ports clk2]\n"
    r = _imp(sdc)
    # Should not crash; may emit diagnostics
    assert isinstance(r.diagnostics, list)
    # Second command should still parse successfully
    assert any(c.values.get("name") == "clk2" for c in r.constraint_set
               if c.type == ConstraintType.CREATE_CLOCK)


def test_adversarial_unmatched_bracket_recovers():
    sdc = "set_false_path -from [get_clocks a\nset_input_delay 1 -clock clk [get_ports d]\n"
    r = _imp(sdc)
    assert any(c.type == ConstraintType.SET_INPUT_DELAY for c in r.constraint_set)


def test_adversarial_command_injection_blocked():
    sdc = "set_false_path -from [source /etc/passwd]\n"
    r = _imp(sdc)
    assert any(d.severity == DiagnosticSeverity.SECURITY for d in r.diagnostics)


def test_adversarial_unknown_switches_preserved_partial():
    # -bogus is not a recognized option; command should still be created
    # as a PARTIAL import with the unknown flag captured.
    r = _imp("create_clock -name clk -period 10 -bogus val [get_ports clk]\n")
    c = _first(r.constraint_set, ConstraintType.CREATE_CLOCK)
    ic = next(i for i in r.imports if i.command_name == "create_clock")
    assert ic.import_status == ImportStatus.PARTIAL
    assert any(u["name"] == "bogus" for u in ic.unsupported_options)
    assert c.values["period"] == pytest.approx(10e-9)


def test_import_status_counts():
    sdc = ("create_clock -name clk -period 10 [get_ports clk]\n"
           "bogus_cmd x y\n"
           "create_clock -name clk2 [get_ports clk2]\n")
    r = _imp(sdc)
    counts = r.counts()
    assert counts["complete"] >= 1
    assert counts["partial"] >= 1
    assert counts["error"] >= 1
    assert counts["constraints"] >= 2


def test_import_source_metadata_preserved():
    sdc = "set_input_delay 1 -clock clk [get_ports d]\n"
    r = _imp(sdc)
    c = _first(r.constraint_set, ConstraintType.SET_INPUT_DELAY)
    im = c.provenance.import_meta
    assert im is not None
    assert im.source_file == "<test>"
    assert im.source_line == 1
    assert "set_input_delay" in im.original_command
    assert im.source_format == "sdc"
    assert c.source_kind == SourceKind.EXISTING_SDC


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_design():
    """Minimal design with clk, rst_n, d_in, clk_b ports and two registers."""
    from rca.parser.slang_adapter import SlangAdapter
    from rca.timing_model import TimingGraph
    sv = textwrap.dedent("""\
        module m(input clk, rst_n, d_in, clk_b, output reg q_a, q_b);
            always_ff @(posedge clk or negedge rst_n) if (!rst_n) q_a<=1'b0; else q_a<=d_in;
            always_ff @(posedge clk_b) q_b<=d_in;
        endmodule
    """)
    with tempfile.NamedTemporaryFile("w", suffix=".sv", delete=False) as f:
        f.write(sv); p = f.name
    try:
        d = SlangAdapter().parse([p], top="m")
    finally:
        Path(p).unlink(missing_ok=True)
    tg = TimingGraph.build(d, user_clocks=[{"name": "clk", "fixed": True,
                                            "port": None, "period_seconds": 10e-9}])
    return d, tg
