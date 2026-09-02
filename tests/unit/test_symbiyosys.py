"""Step 14 — SymbiYosys formal-adapter tests.

The tests use a local executable fixture rather than a real formal tool.  The
fixture writes the status markers emitted by SBY, exercising the adapter's real
subprocess and artifact/provenance boundaries without ever treating a mock
result as a formal proof.
"""
from __future__ import annotations

import textwrap
from copy import deepcopy
from pathlib import Path

import pytest

from rca.config.model import FormalConfig, FormalProofSpec, load_config
from rca.constraint_model import Constraint, ConstraintSet, PathSelector
from rca.design_model import Design
from rca.exceptions import (
    SymbiYosysFormalBackend,
    SymbiYosysProofSpec,
    formal_backend_from_config,
    verify_exceptions,
)
from rca.timing_model import TimingGraph
from rca.timing_model.timing_path import TimingPath
from rca.utils.enums import ConstraintType, ErrorCode, TimingPathClass, VerificationStatus
from rca.validation import validate


def _write_fake_sby(
    tmp_path: Path,
    *,
    status: str | None = "PASS",
    returncode: int = 0,
    sleep_seconds: float = 0.0,
    name: str = "fake-sby",
) -> Path:
    """Create an executable that accepts the SBY argv shape used by RCA."""
    executable = tmp_path / name
    marker = repr(status)
    executable.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import pathlib
            import sys
            import time

            if "--version" in sys.argv:
                print("SBY fake 1.2.3")
                raise SystemExit(0)
            args = sys.argv[1:]
            out_dir = pathlib.Path(args[args.index("-d") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            time.sleep({sleep_seconds!r})
            status = {marker}
            if status:
                (out_dir / status).write_text("")
            if status == "FAIL":
                trace = out_dir / "engine_0" / "trace.vcd"
                trace.parent.mkdir(exist_ok=True)
                trace.write_text("$comment counterexample $end\\n")
            print(f"fake SBY status={{status}}")
            raise SystemExit({returncode})
            """
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _proof_file(tmp_path: Path, name: str = "proof.sby") -> Path:
    proof = tmp_path / name
    proof.write_text("[options]\nmode prove\n", encoding="utf-8")
    return proof


def _backend(
    tmp_path: Path,
    executable: Path,
    *,
    constraint_id: str = "FP1",
    exception_kind: str = "false_path",
    proof: Path | None = None,
    timeout_seconds: int = 5,
) -> SymbiYosysFormalBackend:
    return SymbiYosysFormalBackend(
        [
            SymbiYosysProofSpec(
                constraint_id=constraint_id,
                exception_kind=exception_kind,
                sby_file=proof or _proof_file(tmp_path),
                task="prove_fp",
            )
        ],
        executable=str(executable),
        work_dir=tmp_path / "formal-output",
        timeout_seconds=timeout_seconds,
    )


def _exception_context() -> tuple[ConstraintSet, Design, TimingGraph]:
    cset = ConstraintSet(name="step14")
    cset.add(
        Constraint(
            id="FP1",
            type=ConstraintType.SET_FALSE_PATH,
            path_selector=PathSelector(from_set=["ra/Q"], to_set=["rb/D"]),
            scenario_ids=["FUNC_SLOW"],
        )
    )
    tg = TimingGraph(
        paths=[
            TimingPath(
                startpoint="ra/Q",
                endpoint="rb/D",
                launch_clock="clk",
                capture_clock="clk",
                path_type=TimingPathClass.REG_TO_REG,
            )
        ]
    )
    return cset, Design(name="dut"), tg


def test_14_01_sby_pass_is_the_only_verified_outcome(tmp_path: Path) -> None:
    backend = _backend(tmp_path, _write_fake_sby(tmp_path, status="PASS"))

    result = backend.prove_false_path("FP1", {"from_set": ["ra/Q"], "to_set": ["rb/D"]})

    assert result.status == VerificationStatus.VERIFIED
    assert result.tool == "symbiyosys"
    assert result.tool_version == "SBY fake 1.2.3"
    assert result.property_checked == "false_path:FP1"
    assert result.evidence["sby_file_sha256"]
    assert result.evidence["status_markers"] == [{"status": "PASS", "path": "PASS"}]
    assert result.evidence["argv"][1] == "-f"
    assert Path(result.evidence["run_dir"]).is_dir()


def test_14_02_sby_fail_is_invalid_and_preserves_counterexample_artifacts(tmp_path: Path) -> None:
    backend = _backend(tmp_path, _write_fake_sby(tmp_path, status="FAIL", returncode=1))

    result = backend.prove_false_path("FP1", {})

    assert result.status == VerificationStatus.INVALID
    assert result.counterexample is not None
    assert result.counterexample["status"] == "FAIL"
    assert result.counterexample["artifacts"] == ["engine_0/trace.vcd"]
    assert result.evidence["returncode"] == 1


def test_14_03_sby_unknown_or_indeterminate_output_never_claims_a_proof(tmp_path: Path) -> None:
    unknown_backend = _backend(
        tmp_path, _write_fake_sby(tmp_path, status="UNKNOWN", returncode=2, name="unknown-sby")
    )
    indeterminate_backend = _backend(
        tmp_path,
        _write_fake_sby(tmp_path, status=None, name="indeterminate-sby"),
        proof=_proof_file(tmp_path, "indeterminate.sby"),
    )

    unknown = unknown_backend.prove_false_path("FP1", {})
    indeterminate = indeterminate_backend.prove_false_path("FP1", {})

    assert unknown.status == VerificationStatus.UNRESOLVED
    assert unknown.evidence["reason"] == "sby_unknown"
    assert indeterminate.status == VerificationStatus.UNRESOLVED
    assert indeterminate.evidence["reason"] == "missing_status_marker"


def test_14_04_missing_mapping_file_or_tool_stays_unresolved(tmp_path: Path) -> None:
    executable = _write_fake_sby(tmp_path)
    no_mapping = SymbiYosysFormalBackend(
        [], executable=str(executable), work_dir=tmp_path / "output"
    )
    missing_file = _backend(
        tmp_path,
        executable,
        proof=tmp_path / "missing.sby",
    )
    missing_tool = _backend(
        tmp_path,
        tmp_path / "not-installed-sby",
        proof=_proof_file(tmp_path, "available.sby"),
    )

    assert no_mapping.prove_false_path("FP1", {}).status == VerificationStatus.UNRESOLVED
    assert no_mapping.prove_false_path("FP1", {}).evidence["reason"] == "missing_proof_mapping"
    assert missing_file.prove_false_path("FP1", {}).evidence["reason"] == "missing_proof_file"
    assert missing_tool.prove_false_path("FP1", {}).evidence["reason"] == "tool_unavailable"


def test_14_05_timeout_and_inconsistent_pass_are_not_verified(tmp_path: Path) -> None:
    timeout_backend = _backend(
        tmp_path,
        _write_fake_sby(tmp_path, status="PASS", sleep_seconds=1.0, name="timeout-sby"),
        timeout_seconds=1,
    )
    inconsistent_backend = _backend(
        tmp_path,
        _write_fake_sby(tmp_path, status="PASS", returncode=4, name="inconsistent-sby"),
        proof=_proof_file(tmp_path, "inconsistent.sby"),
    )

    timed_out = timeout_backend.prove_false_path("FP1", {})
    inconsistent = inconsistent_backend.prove_false_path("FP1", {})

    assert timed_out.status == VerificationStatus.UNRESOLVED
    assert timed_out.evidence["reason"] == "timeout"
    assert inconsistent.status == VerificationStatus.ERROR
    assert inconsistent.evidence["reason"] == "inconsistent_process_status"


def test_14_06_kind_mismatch_is_an_explicit_backend_error(tmp_path: Path) -> None:
    backend = _backend(
        tmp_path,
        _write_fake_sby(tmp_path),
        exception_kind="multicycle",
    )

    result = backend.prove_false_path("FP1", {})

    assert result.status == VerificationStatus.ERROR
    assert result.evidence["reason"] == "proof_kind_mismatch"


def test_14_07_multicycle_pass_preserves_cycles_in_identity_and_evidence(tmp_path: Path) -> None:
    backend = _backend(
        tmp_path,
        _write_fake_sby(tmp_path),
        constraint_id="MC1",
        exception_kind="multicycle",
    )

    result = backend.prove_multicycle("MC1", {"from_set": ["ra/Q"]}, cycles=3)

    assert result.status == VerificationStatus.VERIFIED
    assert result.property_checked == "multicycle:MC1:3"
    assert result.evidence["cycles"] == 3


def test_14_08_run_identity_is_stable_and_ucm_remains_immutable(tmp_path: Path) -> None:
    backend = _backend(tmp_path, _write_fake_sby(tmp_path))
    cset, design, tg = _exception_context()
    before = deepcopy(cset.to_snapshot_dict())

    first = backend.prove_false_path("FP1", {"from_set": ["ra/Q"]})
    second = backend.prove_false_path("FP1", {"from_set": ["ra/Q"]})
    verified = verify_exceptions(cset, design=design, tg=tg, backend=backend)

    assert first.status == second.status == VerificationStatus.VERIFIED
    assert first.evidence["run_id"] == second.evidence["run_id"]
    assert first.evidence["run_dir"] == second.evidence["run_dir"]
    assert verified.results[0].verification_status == VerificationStatus.VERIFIED
    assert verified.results[0].verification.evidence["path_spec"]["scenario_ids"] == ["FUNC_SLOW"]
    assert cset.to_snapshot_dict() == before


def test_14_09_validation_uses_supplied_backend_and_blocks_formal_counterexample(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path, _write_fake_sby(tmp_path, status="FAIL", returncode=1))
    cset, design, tg = _exception_context()
    before = deepcopy(cset.to_snapshot_dict())

    result = validate(design=design, tg=tg, cset=cset, formal_backend=backend)

    issue = next(
        issue
        for issue in result.report.issues
        if issue.code == ErrorCode.EXCEPTION_FORMAL_INVALID
    )
    assert result.status == "BLOCKED"
    assert issue.blocking is True
    assert issue.resolution_status == "RESOLVED"
    assert issue.evidence["verification"]["counterexample"]["artifacts"] == ["engine_0/trace.vcd"]
    assert cset.to_snapshot_dict() == before


def test_14_10_config_resolves_proof_paths_and_constructs_adapter(tmp_path: Path) -> None:
    proof_dir = tmp_path / "formal"
    proof_dir.mkdir()
    proof = _proof_file(proof_dir)
    config_file = tmp_path / "project.yaml"
    config_file.write_text(
        """\
schema_version: "1.0"
project:
  name: formal_demo
  top: dut
sources:
  files: []
formal:
  backend: symbiyosys
  symbiyosys_executable: fake-sby
  work_dir: formal-results
  timeout_seconds: 17
  proofs:
    - constraint_id: FP1
      exception_kind: false_path
      sby_file: formal/proof.sby
      task: prove_fp
""",
        encoding="utf-8",
    )

    config = load_config(config_file)
    backend = formal_backend_from_config(config.formal)

    assert isinstance(backend, SymbiYosysFormalBackend)
    assert config.formal.work_dir == str((tmp_path / "formal-results").resolve())
    assert backend.timeout_seconds == 17
    assert backend.configured_constraint_ids == ("FP1",)
    assert backend._proofs["FP1"].sby_file == proof.resolve()


def test_14_11_formal_config_rejects_duplicate_constraint_mappings() -> None:
    with pytest.raises(ValueError, match="at most one mapping"):
        FormalConfig(
            backend="symbiyosys",
            proofs=[
                FormalProofSpec(
                    constraint_id="FP1", exception_kind="false_path", sby_file="a.sby"
                ),
                FormalProofSpec(
                    constraint_id="FP1", exception_kind="false_path", sby_file="b.sby"
                ),
            ],
        )
