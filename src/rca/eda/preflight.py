"""Typed, vendor-neutral preflight evidence for external EDA boundaries.

Preflight is intentionally observational: it may perform bounded version probes,
but it never invokes synthesis, STA, or proof execution.  It produces evidence
that is useful to a CLI, a real-flow manifest, and tests without becoming a
second flow, QoR, cache, or artifact authority.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from .. import __version__
from ..utils.hashing import stable_hash
from .base import ToolInfo


class CapabilityClassification(str, Enum):
    """Coarse operator-facing classifications for one capability check.

    This intentionally lives inside the preflight evidence model rather than
    becoming a second execution-state authority. ``CapabilityStatus`` below
    retains the more precise lifecycle/output vocabulary used by artifacts.
    """

    AVAILABLE = "available"
    TOOL_MISSING = "tool_missing"
    TOOL_UNAVAILABLE = "tool_unavailable"
    INPUT_MISSING = "input_missing"
    LIBERTY_MISSING = "liberty_missing"
    CONFIG_INVALID = "config_invalid"
    BACKEND_UNSUPPORTED = "backend_unsupported"
    ENVIRONMENT_INVALID = "environment_invalid"
    PERMISSION_ERROR = "permission_error"
    PRECHECK_FAILED = "precheck_failed"


class CapabilityStatus(str, Enum):
    """Detailed lifecycle/capability states shared by vendor-neutral EDA checks."""

    EXECUTABLE_MISSING = "executable_missing"
    EXECUTABLE_FOUND = "executable_found"
    VERSION_DISCOVERED = "version_discovered"
    COLLATERAL_MISSING = "collateral_missing"
    CONFIGURATION_INVALID = "configuration_invalid"
    ENVIRONMENT_READY = "environment_ready"
    OUTPUT_LOCATION_READY = "output_location_ready"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_COMPLETED = "execution_completed"
    EXECUTION_FAILED = "execution_failed"
    EXECUTION_TIMED_OUT = "execution_timed_out"
    OUTPUT_MISSING = "output_missing"
    OUTPUT_MALFORMED = "output_malformed"
    PERMISSION_ERROR = "permission_error"
    UNSUPPORTED = "unsupported"
    NOT_REQUIRED = "not_required"
    NOT_DISCOVERABLE = "not_discoverable"


_READY_STATUSES = {
    CapabilityStatus.VERSION_DISCOVERED,
    CapabilityStatus.ENVIRONMENT_READY,
    CapabilityStatus.OUTPUT_LOCATION_READY,
    CapabilityStatus.NOT_REQUIRED,
    CapabilityStatus.NOT_DISCOVERABLE,
}


@dataclass(frozen=True)
class CapabilityCheck:
    """One deterministic preflight finding for an input, tool, or location."""

    component: str
    status: CapabilityStatus
    required: bool = True
    detail: str = ""
    executable: str = ""
    version: str | None = None
    path: str = ""

    @property
    def ready(self) -> bool:
        return not self.required or self.status in _READY_STATUSES

    @property
    def classification(self) -> CapabilityClassification:
        """Map detailed status/component evidence to a stable operator class."""
        if self.status in _READY_STATUSES:
            return CapabilityClassification.AVAILABLE
        if self.status == CapabilityStatus.EXECUTABLE_MISSING:
            return CapabilityClassification.TOOL_MISSING
        if self.status == CapabilityStatus.EXECUTABLE_FOUND:
            return CapabilityClassification.TOOL_UNAVAILABLE
        if self.status == CapabilityStatus.COLLATERAL_MISSING:
            return (CapabilityClassification.LIBERTY_MISSING if self.component == "liberty"
                    else CapabilityClassification.INPUT_MISSING)
        if self.status == CapabilityStatus.CONFIGURATION_INVALID:
            return (CapabilityClassification.ENVIRONMENT_INVALID
                    if self.component in {"output_directory", "expected_outputs"}
                    else CapabilityClassification.CONFIG_INVALID)
        if self.status == CapabilityStatus.PERMISSION_ERROR:
            return CapabilityClassification.PERMISSION_ERROR
        if self.status == CapabilityStatus.UNSUPPORTED:
            return CapabilityClassification.BACKEND_UNSUPPORTED
        return CapabilityClassification.PRECHECK_FAILED

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "status": self.status.value,
            "classification": self.classification.value,
            "required": self.required,
            "ready": self.ready,
            "detail": self.detail,
            "executable": self.executable,
            "version": self.version,
            "path": self.path,
        }


@dataclass(frozen=True)
class EDAPreflight:
    """Auditable readiness snapshot for one selected EDA boundary."""

    backend: str
    checks: tuple[CapabilityCheck, ...] = ()
    environment_fingerprint: str = ""
    kind: str = "real_eda_preflight"
    schema_version: int = 1

    @property
    def ready(self) -> bool:
        return all(check.ready for check in self.checks)

    @property
    def overall_status(self) -> CapabilityStatus:
        return (CapabilityStatus.ENVIRONMENT_READY if self.ready
                else CapabilityStatus.CONFIGURATION_INVALID)

    @property
    def failure_classification(self) -> str:
        for check in self.checks:
            if check.required and not check.ready:
                return check.status.value
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "backend": self.backend,
            "ready": self.ready,
            "overall_status": self.overall_status.value,
            "failure_classification": self.failure_classification or None,
            "environment_fingerprint": self.environment_fingerprint,
            "checks": [check.to_dict() for check in self.checks],
        }


def _executable_found(executable: str) -> bool:
    if not executable:
        return False
    path = Path(executable)
    return bool((path.is_file() and os.access(path, os.X_OK)) or shutil.which(executable))


def _tool_check(component: str, info: ToolInfo | None) -> CapabilityCheck:
    """Classify a required executable without turning a failed probe into ready.

    ``ToolInfo.available`` means the backend's bounded argv-only version probe
    completed successfully.  A file merely being executable is useful
    diagnostic evidence, but is deliberately not enough to launch a real run:
    a broken wrapper must fail closed before synthesis or STA starts.
    """
    executable = info.executable if info else ""
    if not _executable_found(executable):
        return CapabilityCheck(
            component=component,
            status=CapabilityStatus.EXECUTABLE_MISSING,
            executable=executable,
            detail=(info.error if info else "tool was not discovered") or "executable was not found",
        )
    version = info.version if info else None
    if info and info.available:
        return CapabilityCheck(
            component=component,
            status=CapabilityStatus.VERSION_DISCOVERED,
            executable=executable,
            version=version,
            detail="executable found; bounded version probe completed",
        )
    return CapabilityCheck(
        component=component,
        status=CapabilityStatus.EXECUTABLE_FOUND,
        executable=executable,
        version=version,
        detail=(info.error if info and info.error
                else "executable found but the bounded version probe did not complete"),
    )


def _is_writable_or_creatable(directory: Path) -> bool:
    """Check output-parent viability without creating an inspection artifact."""
    candidate = directory
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.W_OK)


def _readable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.R_OK)


def environment_fingerprint(*, backend: str, tools: dict[str, ToolInfo | None],
                            liberty_hashes: dict[str, str], config_hash: str) -> str:
    """Return stable provenance identity without timestamps, PIDs, paths, or secrets."""
    tool_data = {
        name: {
            "tool": info.tool if info else name,
            "vendor": info.vendor if info else "",
            "version": info.version if info else None,
        }
        for name, info in sorted(tools.items())
    }
    return stable_hash({
        "schema": "rca-real-eda-environment-v1",
        "rca_version": __version__,
        "python": ".".join(map(str, sys.version_info[:3])),
        "backend": backend,
        "tools": tool_data,
        "liberty_hashes": dict(sorted(liberty_hashes.items())),
        "configuration_hash": config_hash,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
    })


def preflight_yosys_opensta(*, backend: str, top: str, sources: list[Path],
                             liberty: list[Path], sdc_path: Path | None,
                             output_dir: Path, expected_outputs: list[Path],
                             yosys_info: ToolInfo | None, opensta_info: ToolInfo | None,
                             liberty_hashes: dict[str, str], config_hash: str,
                             include_dirs: list[Path] | None = None) -> EDAPreflight:
    """Check real Yosys/OpenSTA prerequisites without running either tool."""
    checks: list[CapabilityCheck] = []
    if backend != "yosys_opensta":
        checks.append(CapabilityCheck(
            component="backend", status=CapabilityStatus.UNSUPPORTED,
            detail=f"Real execution is not implemented for backend {backend!r}; select mock explicitly or yosys_opensta.",
        ))
    elif not top:
        checks.append(CapabilityCheck(
            component="backend", status=CapabilityStatus.CONFIGURATION_INVALID,
            detail="top module is empty",
        ))
    else:
        checks.append(CapabilityCheck(
            component="backend", status=CapabilityStatus.ENVIRONMENT_READY,
            detail="selected real backend is yosys_opensta",
        ))
    checks.extend((_tool_check("yosys", yosys_info), _tool_check("opensta", opensta_info)))

    missing_sources = [str(path) for path in sources if not path.is_file()]
    unreadable_sources = [str(path) for path in sources if path.is_file() and not os.access(path, os.R_OK)]
    checks.append(CapabilityCheck(
        component="rtl_sources",
        status=(CapabilityStatus.ENVIRONMENT_READY if sources and not missing_sources and not unreadable_sources
                else CapabilityStatus.PERMISSION_ERROR if unreadable_sources
                else CapabilityStatus.COLLATERAL_MISSING),
        detail=("all configured RTL sources are readable" if sources and not missing_sources and not unreadable_sources
                else "unreadable RTL source set: " + ", ".join(unreadable_sources)
                if unreadable_sources else "missing or empty RTL source set: "
                + ", ".join(missing_sources or ["<none>"])),
        path=",".join(str(path) for path in sources),
    ))
    missing_liberty = [str(path) for path in liberty if not path.is_file()]
    unreadable_liberty = [str(path) for path in liberty if path.is_file() and not os.access(path, os.R_OK)]
    checks.append(CapabilityCheck(
        component="liberty",
        status=(CapabilityStatus.ENVIRONMENT_READY if liberty and not missing_liberty and not unreadable_liberty
                else CapabilityStatus.PERMISSION_ERROR if unreadable_liberty
                else CapabilityStatus.COLLATERAL_MISSING),
        detail=("all configured Liberty files are readable" if liberty and not missing_liberty and not unreadable_liberty
                else "unreadable Liberty set: " + ", ".join(unreadable_liberty)
                if unreadable_liberty else "missing or empty Liberty set: "
                + ", ".join(missing_liberty or ["<none>"])),
        path=",".join(str(path) for path in liberty),
    ))
    include_dirs = include_dirs or []
    missing_include_dirs = [str(path) for path in include_dirs if not path.is_dir()]
    unreadable_include_dirs = [str(path) for path in include_dirs
                               if path.is_dir() and not os.access(path, os.R_OK)]
    checks.append(CapabilityCheck(
        component="include_directories",
        status=(CapabilityStatus.ENVIRONMENT_READY if not missing_include_dirs and not unreadable_include_dirs
                else CapabilityStatus.PERMISSION_ERROR if unreadable_include_dirs
                else CapabilityStatus.COLLATERAL_MISSING),
        detail=("all configured include directories are readable" if not missing_include_dirs and not unreadable_include_dirs
                else "unreadable include directories: " + ", ".join(unreadable_include_dirs)
                if unreadable_include_dirs else "missing include directories: "
                + ", ".join(missing_include_dirs)),
        path=",".join(str(path) for path in include_dirs),
    ))
    sdc_missing = not sdc_path or not sdc_path.is_file()
    sdc_unreadable = bool(sdc_path and sdc_path.is_file() and not os.access(sdc_path, os.R_OK))
    checks.append(CapabilityCheck(
        component="generated_sdc",
        status=(CapabilityStatus.ENVIRONMENT_READY if not sdc_missing and not sdc_unreadable
                else CapabilityStatus.PERMISSION_ERROR if sdc_unreadable
                else CapabilityStatus.COLLATERAL_MISSING),
        detail=("generated SDC is present" if not sdc_missing and not sdc_unreadable
                else "generated SDC is unreadable" if sdc_unreadable else "generated SDC is missing"),
        path=str(sdc_path or ""),
    ))
    writable = _is_writable_or_creatable(output_dir)
    checks.append(CapabilityCheck(
        component="output_directory",
        status=CapabilityStatus.OUTPUT_LOCATION_READY if writable else CapabilityStatus.CONFIGURATION_INVALID,
        detail="output directory is writable or can be created" if writable else "output directory is not writable",
        path=str(output_dir),
    ))
    invalid_output_paths = [str(path) for path in expected_outputs if path.exists() and not path.is_file()]
    output_parent_ready = (not invalid_output_paths and
                           all(_is_writable_or_creatable(path.parent) for path in expected_outputs))
    checks.append(CapabilityCheck(
        component="expected_outputs",
        status=(CapabilityStatus.OUTPUT_LOCATION_READY if output_parent_ready
                else CapabilityStatus.CONFIGURATION_INVALID),
        detail=("isolated output locations are writable or can be created" if output_parent_ready
                else ("expected output path is not a file: " + ", ".join(invalid_output_paths)
                      if invalid_output_paths else "one or more expected output locations are not writable")),
        path=",".join(str(path) for path in expected_outputs),
    ))
    fingerprint = environment_fingerprint(
        backend=backend,
        tools={"yosys": yosys_info, "opensta": opensta_info},
        liberty_hashes=liberty_hashes,
        config_hash=config_hash,
    )
    return EDAPreflight(backend=backend, checks=tuple(checks), environment_fingerprint=fingerprint)


def preflight_symbiyosys(*, executable: str, version: str | None,
                          proofs: list[Path], sources: list[Path],
                          config_hash: str = "", required: bool = True) -> EDAPreflight:
    """Check explicit SymbiYosys collateral without proving anything."""
    info = ToolInfo(
        vendor="SymbiYosys", tool="symbiyosys", version=version or "unknown",
        executable=executable,
        available=bool(version and version != "unknown"),
    )
    checks = [replace(_tool_check("symbiyosys", info), required=required)]
    missing_proofs = [str(path) for path in proofs if not _readable_file(path)]
    checks.append(CapabilityCheck(
        component="sby_collateral",
        status=(CapabilityStatus.ENVIRONMENT_READY if proofs and not missing_proofs
                else CapabilityStatus.COLLATERAL_MISSING),
        required=required,
        detail=("all explicit .sby files are readable" if proofs and not missing_proofs
                else "missing or empty .sby collateral: " + ", ".join(missing_proofs or ["<none>"])),
        path=",".join(str(path) for path in proofs),
    ))
    missing_sources = [str(path) for path in sources if not _readable_file(path)]
    checks.append(CapabilityCheck(
        component="rtl_sources",
        status=(CapabilityStatus.ENVIRONMENT_READY if sources and not missing_sources
                else CapabilityStatus.COLLATERAL_MISSING),
        required=required,
        detail=("all configured RTL sources are readable" if sources and not missing_sources
                else "missing or empty RTL source set: " + ", ".join(missing_sources or ["<none>"])),
        path=",".join(str(path) for path in sources),
    ))
    checks.append(CapabilityCheck(
        component="solver",
        status=CapabilityStatus.NOT_DISCOVERABLE,
        required=False,
        detail="solver selection is defined by explicit .sby collateral; preflight does not run it",
    ))
    fingerprint = environment_fingerprint(
        backend="symbiyosys",
        tools={"symbiyosys": info},
        liberty_hashes={},
        config_hash=config_hash,
    )
    return EDAPreflight(backend="symbiyosys", checks=tuple(checks),
                        environment_fingerprint=fingerprint, kind="formal_preflight")
