"""SymbiYosys-backed formal verification for timing exceptions (Step 14).

This adapter deliberately does *not* translate an RCA path selector into a
formal assertion.  An SDC exception describes timing intent; a proof of that
intent requires design-specific temporal assumptions and properties.  Those
properties therefore remain user-authored ``.sby`` jobs and are mapped
explicitly to UCM constraint IDs.

Only an observed SymbiYosys ``PASS`` marker together with a zero process exit
code yields :class:`~rca.utils.enums.VerificationStatus.VERIFIED`.  Missing
mapping/tool/job, a timeout, or an indeterminate job remain ``UNRESOLVED``;
they can never be promoted to a proof by structural analysis alone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..utils.enums import VerificationStatus
from ..utils.hashing import hash_file, stable_hash
from .formal_backend import FormalBackend, VerificationResult

_STATUS_MARKERS = ("PASS", "FAIL", "UNKNOWN", "ERROR")
_OUTPUT_TAIL_LIMIT = 4000
_COUNTEREXAMPLE_LIMIT = 20


@dataclass(frozen=True)
class SymbiYosysProofSpec:
    """One explicit mapping between an RCA exception and an ``.sby`` proof.

    ``exception_kind`` is intentionally required.  It prevents, for example,
    a proof written for a false-path claim from being silently re-used after a
    constraint changes into a multicycle exception.
    """

    constraint_id: str
    exception_kind: str  # ``false_path`` or ``multicycle``
    sby_file: Path
    task: str | None = None

    def to_identity_dict(self) -> dict[str, str | None]:
        return {
            "constraint_id": self.constraint_id,
            "exception_kind": self.exception_kind,
            "sby_file": str(self.sby_file),
            "task": self.task,
        }


class SymbiYosysFormalBackend(FormalBackend):
    """Run explicitly-configured SymbiYosys proof jobs safely.

    The adapter invokes ``sby`` with an argument list (never a shell), uses a
    stable, constraint-specific output directory below ``work_dir``, and
    retains bounded invocation evidence.  ``sby -f`` is limited to that
    derived directory; the caller-owned proof source is never modified.
    """

    name = "symbiyosys"
    default_binary_name = "sby"
    env_var = "RCA_SYMBIYOSYS"

    def __init__(
        self,
        proofs: Iterable[SymbiYosysProofSpec] | None = None,
        *,
        work_dir: str | Path = "output/formal",
        executable: str | None = None,
        timeout_seconds: int = 300,
    ) -> None:
        if timeout_seconds < 1:
            raise ValueError("SymbiYosys timeout_seconds must be at least 1")
        self.work_dir = Path(work_dir)
        self.executable = executable or self._resolve_executable()
        self.timeout_seconds = int(timeout_seconds)
        self.version: str | None = None
        self._version_probed = False
        self._proofs: dict[str, SymbiYosysProofSpec] = {}
        for proof in proofs or []:
            if proof.exception_kind not in {"false_path", "multicycle"}:
                raise ValueError(
                    "SymbiYosys proof exception_kind must be 'false_path' or "
                    f"'multicycle', got {proof.exception_kind!r}"
                )
            if not proof.constraint_id:
                raise ValueError("SymbiYosys proof constraint_id must not be empty")
            if proof.task is not None and (
                not proof.task.strip() or proof.task.strip().startswith("-")
            ):
                raise ValueError("SymbiYosys proof task must be a non-option task name")
            if proof.constraint_id in self._proofs:
                raise ValueError(
                    f"More than one SymbiYosys proof is configured for "
                    f"constraint '{proof.constraint_id}'"
                )
            self._proofs[proof.constraint_id] = proof

    @property
    def configured_constraint_ids(self) -> tuple[str, ...]:
        """Sorted proof-mapping IDs; useful for diagnostics and tests."""
        return tuple(sorted(self._proofs))

    def prove_false_path(
        self, constraint_id: str, path_spec: dict[str, Any]
    ) -> VerificationResult:
        return self._run_proof(constraint_id, "false_path", path_spec, cycles=None)

    def prove_multicycle(
        self, constraint_id: str, path_spec: dict[str, Any], cycles: int
    ) -> VerificationResult:
        return self._run_proof(constraint_id, "multicycle", path_spec, cycles=cycles)

    # ------------------------------------------------------------------
    # Tool discovery and execution
    # ------------------------------------------------------------------

    def _resolve_executable(self) -> str:
        from_env = os.environ.get(self.env_var)
        if from_env:
            found = shutil.which(from_env)
            return found or from_env
        found = shutil.which(self.default_binary_name)
        return found or self.default_binary_name

    def _executable_available(self) -> bool:
        executable_path = Path(self.executable)
        return bool(
            (executable_path.is_file() and os.access(executable_path, os.X_OK))
            or shutil.which(self.executable)
        )

    def get_version(self) -> str | None:
        """Best-effort version discovery; failure never pretends no proof ran."""
        if self._version_probed:
            return self.version
        self._version_probed = True
        if not self._executable_available():
            return None
        try:
            proc = subprocess.run(
                [self.executable, "--version"],
                cwd=str(self.work_dir.resolve()),
                capture_output=True,
                text=True,
                timeout=min(15, self.timeout_seconds),
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        if proc.returncode == 0 and output:
            self.version = output.splitlines()[0][:200]
        return self.version

    def _run_proof(
        self,
        constraint_id: str,
        expected_kind: str,
        path_spec: dict[str, Any],
        *,
        cycles: int | None,
    ) -> VerificationResult:
        proof = self._proofs.get(constraint_id)
        canonical_path_spec = _canonicalize(path_spec)
        base_evidence: dict[str, Any] = {
            "adapter": self.name,
            "expected_exception_kind": expected_kind,
            "configured_constraint_ids": list(self.configured_constraint_ids),
            "path_spec": canonical_path_spec,
        }
        if cycles is not None:
            base_evidence["cycles"] = cycles
        if proof is None:
            return self._unresolved(
                constraint_id,
                expected_kind,
                base_evidence,
                "No explicit SymbiYosys proof mapping is configured for this exception.",
                reason="missing_proof_mapping",
            )
        if proof.exception_kind != expected_kind:
            evidence = {
                **base_evidence,
                "proof": proof.to_identity_dict(),
                "reason": "proof_kind_mismatch",
            }
            return VerificationResult(
                constraint_id=constraint_id,
                source_constraint_id=constraint_id,
                status=VerificationStatus.ERROR,
                property_checked=self._property_name(expected_kind, constraint_id, cycles),
                evidence=evidence,
                tool=self.name,
                tool_version=self.get_version(),
                message=(
                    f"Configured proof for {constraint_id!r} is {proof.exception_kind!r}, "
                    f"but verification requested {expected_kind!r}."
                ),
            )

        sby_file = proof.sby_file.resolve()
        evidence = {
            **base_evidence,
            "proof": proof.to_identity_dict(),
            "sby_file": str(sby_file),
            "task": proof.task,
        }
        if not sby_file.is_file():
            return self._unresolved(
                constraint_id,
                expected_kind,
                evidence,
                f"Configured SymbiYosys proof file does not exist: {sby_file}",
                reason="missing_proof_file",
                cycles=cycles,
            )
        if not self._executable_available():
            return self._unresolved(
                constraint_id,
                expected_kind,
                evidence,
                f"SymbiYosys executable is unavailable: {self.executable}",
                reason="tool_unavailable",
                cycles=cycles,
            )

        sby_hash = hash_file(sby_file)
        run_id = stable_hash(
            {
                "adapter": "rca-symbiyosys-v1",
                "constraint_id": constraint_id,
                "exception_kind": expected_kind,
                # The source location is part of identity because an .sby file
                # may include adjacent RTL/formal collateral by relative path.
                "sby_file": str(sby_file),
                "sby_file_sha256": sby_hash,
                "task": proof.task,
                "path_spec": canonical_path_spec,
                "cycles": cycles,
            }
        )[:20]
        work_root = self.work_dir.resolve()
        run_dir = (work_root / f"{_safe_component(constraint_id)}-{run_id}").resolve()
        # ``run_dir`` is generated from a safe component, but retain a
        # containment check before passing -f to an external tool.
        if not _is_within(run_dir, work_root):
            return VerificationResult(
                constraint_id=constraint_id,
                source_constraint_id=constraint_id,
                status=VerificationStatus.ERROR,
                property_checked=self._property_name(expected_kind, constraint_id, cycles),
                evidence={**evidence, "reason": "unsafe_work_directory"},
                tool=self.name,
                tool_version=self.get_version(),
                message="Refusing to run SymbiYosys outside the configured formal work directory.",
            )
        work_root.mkdir(parents=True, exist_ok=True)
        version = self.get_version()
        argv = [self.executable, "-f", "-d", str(run_dir), str(sby_file)]
        if proof.task:
            argv.append(proof.task)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(sby_file.parent),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                shell=False,
                check=False,
            )
            duration = time.monotonic() - started
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started
            evidence.update(
                {
                    "reason": "timeout",
                    "run_id": run_id,
                    "run_dir": str(run_dir),
                    "argv": argv,
                    "timeout_seconds": self.timeout_seconds,
                    "stdout_tail": _tail(exc.stdout),
                    "stderr_tail": _tail(exc.stderr),
                }
            )
            return self._unresolved(
                constraint_id,
                expected_kind,
                evidence,
                f"SymbiYosys proof timed out after {self.timeout_seconds}s.",
                reason="timeout",
                cycles=cycles,
                runtime_seconds=duration,
                tool_version=version,
            )
        except OSError as exc:
            duration = time.monotonic() - started
            evidence.update(
                {
                    "reason": "tool_execution_error",
                    "run_id": run_id,
                    "run_dir": str(run_dir),
                    "argv": argv,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return self._unresolved(
                constraint_id,
                expected_kind,
                evidence,
                "SymbiYosys could not be executed; formal proof remains unresolved.",
                reason="tool_execution_error",
                cycles=cycles,
                runtime_seconds=duration,
                tool_version=version,
            )

        markers = _read_status_markers(run_dir)
        marker_statuses = sorted({status for status, _ in markers})
        evidence.update(
            {
                "run_id": run_id,
                "run_dir": str(run_dir),
                "sby_file_sha256": sby_hash,
                "argv": argv,
                "returncode": proc.returncode,
                "status_markers": [
                    {"status": status, "path": path} for status, path in markers
                ],
                "stdout_tail": _tail(proc.stdout),
                "stderr_tail": _tail(proc.stderr),
            }
        )
        property_checked = self._property_name(expected_kind, constraint_id, cycles)

        if not marker_statuses:
            return self._unresolved(
                constraint_id,
                expected_kind,
                evidence,
                "SymbiYosys produced no recognizable status marker; formal proof remains unresolved.",
                reason="missing_status_marker",
                cycles=cycles,
                runtime_seconds=duration,
                tool_version=version,
            )
        if len(marker_statuses) > 1:
            evidence["reason"] = "ambiguous_status_marker"
            return VerificationResult(
                constraint_id=constraint_id,
                source_constraint_id=constraint_id,
                status=VerificationStatus.ERROR,
                property_checked=property_checked,
                evidence=evidence,
                tool=self.name,
                tool_version=version,
                runtime_seconds=duration,
                message=(
                    "SymbiYosys produced conflicting status markers; RCA cannot "
                    "classify this proof result."
                ),
            )

        status = marker_statuses[0]
        if status == "PASS" and proc.returncode == 0:
            return VerificationResult(
                constraint_id=constraint_id,
                source_constraint_id=constraint_id,
                status=VerificationStatus.VERIFIED,
                property_checked=property_checked,
                evidence=evidence,
                tool=self.name,
                tool_version=version,
                runtime_seconds=duration,
                message="SymbiYosys completed the configured proof with PASS.",
            )
        if status == "FAIL":
            counterexample = {
                "status": "FAIL",
                "artifacts": _counterexample_artifacts(run_dir),
                "message": "SymbiYosys reported FAIL; inspect preserved artifact paths.",
            }
            return VerificationResult(
                constraint_id=constraint_id,
                source_constraint_id=constraint_id,
                status=VerificationStatus.INVALID,
                property_checked=property_checked,
                evidence=evidence,
                counterexample=counterexample,
                tool=self.name,
                tool_version=version,
                runtime_seconds=duration,
                message="SymbiYosys found a counterexample to the configured proof.",
            )
        if status == "UNKNOWN":
            return self._unresolved(
                constraint_id,
                expected_kind,
                evidence,
                "SymbiYosys reported UNKNOWN; formal proof remains unresolved.",
                reason="sby_unknown",
                cycles=cycles,
                runtime_seconds=duration,
                tool_version=version,
            )

        # ERROR marker, PASS with non-zero exit, and any future marker value
        # are not a proof and are distinct from a normal unknown result.
        evidence["reason"] = "sby_error" if status == "ERROR" else "inconsistent_process_status"
        return VerificationResult(
            constraint_id=constraint_id,
            source_constraint_id=constraint_id,
            status=VerificationStatus.ERROR,
            property_checked=property_checked,
            evidence=evidence,
            tool=self.name,
            tool_version=version,
            runtime_seconds=duration,
            message=(
                "SymbiYosys reported ERROR."
                if status == "ERROR"
                else "SymbiYosys reported PASS but exited non-zero; refusing to claim a proof."
            ),
        )

    def _unresolved(
        self,
        constraint_id: str,
        expected_kind: str,
        evidence: dict[str, Any],
        message: str,
        *,
        reason: str,
        cycles: int | None = None,
        runtime_seconds: float | None = None,
        tool_version: str | None = None,
    ) -> VerificationResult:
        merged_evidence = dict(evidence)
        merged_evidence["reason"] = reason
        return VerificationResult(
            constraint_id=constraint_id,
            source_constraint_id=constraint_id,
            status=VerificationStatus.UNRESOLVED,
            property_checked=self._property_name(expected_kind, constraint_id, cycles),
            evidence=merged_evidence,
            tool=self.name,
            tool_version=tool_version if tool_version is not None else self.get_version(),
            runtime_seconds=runtime_seconds,
            message=message,
        )

    @staticmethod
    def _property_name(kind: str, constraint_id: str, cycles: int | None) -> str:
        suffix = f":{cycles}" if cycles is not None else ""
        return f"{kind}:{constraint_id}{suffix}"


def formal_backend_from_config(config: Any) -> FormalBackend:
    """Build the configured formal backend without coupling core validation to YAML.

    ``config`` is intentionally duck-typed so ``rca.exceptions`` does not
    import the Pydantic configuration package.  That preserves the existing
    formal abstraction for library callers and unit tests.
    """
    backend_name = str(getattr(config, "backend", "conservative"))
    if backend_name == "conservative":
        from .formal_backend import ConservativeFormalBackend

        return ConservativeFormalBackend()
    if backend_name != "symbiyosys":
        raise ValueError(f"Unsupported formal backend: {backend_name!r}")

    proof_specs: list[SymbiYosysProofSpec] = []
    for proof in list(getattr(config, "proofs", []) or []):
        proof_specs.append(
            SymbiYosysProofSpec(
                constraint_id=str(proof.constraint_id),
                exception_kind=str(proof.exception_kind),
                sby_file=Path(proof.sby_file),
                task=getattr(proof, "task", None),
            )
        )
    return SymbiYosysFormalBackend(
        proof_specs,
        work_dir=getattr(config, "work_dir", "output/formal"),
        executable=getattr(config, "symbiyosys_executable", None),
        timeout_seconds=int(getattr(config, "timeout_seconds", 300)),
    )


def _canonicalize(value: Any) -> Any:
    """Make proof-input provenance and run identity deterministic and JSON-safe.

    Selector stages are normally lists and retain their order. Sets are the
    sole unordered collection accepted from library callers and are sorted by
    their canonical hash instead of relying on process-randomised ``repr``.
    """
    if isinstance(value, dict):
        return {str(key): _canonicalize(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_canonicalize(item) for item in value]
        return sorted(normalized, key=stable_hash)
    if isinstance(value, Path):
        return str(value)
    enum_value = getattr(value, "value", None)
    if enum_value is not None and not isinstance(value, (str, bytes)):
        return _canonicalize(enum_value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _safe_component(value: str) -> str:
    """Return a deterministic safe path component without trusting a UCM ID."""
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in value)
    return cleaned[:80] or "exception"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _tail(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value[-_OUTPUT_TAIL_LIMIT:]


def _read_status_markers(run_dir: Path) -> list[tuple[str, str]]:
    """Read bounded root/task status markers emitted by current SBY versions."""
    markers: list[tuple[str, str]] = []
    locations = [run_dir]
    if run_dir.is_dir():
        locations.extend(sorted(p for p in run_dir.iterdir() if p.is_dir()))
    for location in locations:
        for status in _STATUS_MARKERS:
            marker = location / status
            if marker.is_file():
                markers.append((status, str(marker.relative_to(run_dir))))
        status_file = location / "status"
        if status_file.is_file():
            try:
                content = status_file.read_text(encoding="utf-8", errors="replace").strip().upper()
            except OSError:
                content = ""
            if content in _STATUS_MARKERS:
                markers.append((content, str(status_file.relative_to(run_dir))))
    return sorted(set(markers), key=lambda item: (item[0], item[1]))


def _counterexample_artifacts(run_dir: Path) -> list[str]:
    """Return a bounded, deterministic list of trace-like artifacts under a run."""
    found: list[str] = []
    if not run_dir.is_dir():
        return found
    for root, dirs, files in os.walk(run_dir, followlinks=False):
        dirs.sort()
        for filename in sorted(files):
            lower = filename.lower()
            if not (
                lower.endswith((".vcd", ".fst", ".yw"))
                or "trace" in lower
                or "cex" in lower
                or "counterexample" in lower
            ):
                continue
            path = Path(root) / filename
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if not _is_within(resolved, run_dir.resolve()):
                continue
            found.append(str(path.relative_to(run_dir)))
            if len(found) >= _COUNTEREXAMPLE_LIMIT:
                return found
    return found
