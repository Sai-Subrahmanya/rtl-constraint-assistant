#!/usr/bin/env python3
"""Inspect RCA EDA prerequisites without running synthesis, STA, or formal jobs.

AVAILABLE means an executable answered a lightweight version probe. It is not a
claim that a full flow, license, library, design, or tool invocation is usable.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from rca.config.model import load_config
from rca.eda import OpenSTABackend, YosysBackend
from rca.exceptions import SymbiYosysFormalBackend


def _tool_record(info) -> dict[str, Any]:
    return {
        "status": "AVAILABLE" if info.available else "MISSING",
        "executable": info.executable,
        "version": info.version,
        "detail": info.error,
        "probe_only": True,
    }


def _report(config_path: str | None) -> tuple[dict[str, Any], int]:
    cfg = None
    config_detail = "not configured"
    if config_path:
        try:
            cfg = load_config(Path(config_path))
            config_detail = str(Path(config_path).resolve())
        except ValueError as exc:
            return ({"kind": "rca_eda_diagnostic", "configuration": {"status": "INVALID", "detail": str(exc)}}, 2)
    yosys = _tool_record(YosysBackend().discover())
    opensta = _tool_record(OpenSTABackend().discover())
    sby_executable = getattr(cfg.formal, "symbiyosys_executable", None) if cfg else None
    formal = SymbiYosysFormalBackend(executable=sby_executable, work_dir=Path.cwd())
    sby_version = formal.get_version()
    sby = {
        "status": "AVAILABLE" if sby_version else "MISSING",
        "executable": formal.executable,
        "version": sby_version,
        "detail": "version probe only" if sby_version else "executable unavailable or version probe failed",
        "probe_only": True,
    }
    libraries = []
    backend = None
    if cfg:
        backend = cfg.flow.backend
        libraries = [{"path": path, "status": "AVAILABLE" if Path(path).is_file() else "MISSING"}
                     for path in cfg.flow.liberty_files()]
    all_libraries_available = bool(libraries) and all(item["status"] == "AVAILABLE" for item in libraries)
    real_ready = yosys["status"] == "AVAILABLE" and opensta["status"] == "AVAILABLE" and all_libraries_available
    report = {
        "kind": "rca_eda_diagnostic",
        "configuration": {"status": "CONFIGURED" if cfg else "NOT_CONFIGURED", "detail": config_detail},
        "configured_backend": backend,
        "tools": {"yosys": yosys, "opensta": opensta, "symbiyosys": sby},
        "liberty": libraries,
        "real_yosys_opensta_flow_expected_runnable": real_ready,
        "mock_only_supported": True,
        "disclaimer": "Availability is a version-probe result only; it is not a full-flow or signoff claim.",
    }
    return report, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="optional RCA project YAML")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args()
    report, exit_code = _report(args.config)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("RCA EDA DIAGNOSTIC (does not execute synthesis/STA/formal)")
        print(f"Configuration: {report['configuration']['status']} ({report['configuration']['detail']})")
        if exit_code == 0:
            print(f"Configured backend: {report['configured_backend'] or 'not configured'}")
            for name, tool in report["tools"].items():
                text = tool["version"] or tool["detail"] or ""
                print(f"OPTIONAL {name}: {tool['status']} {text}".rstrip())
            if report["liberty"]:
                for library in report["liberty"]:
                    print(f"CONFIGURED Liberty: {library['status']} {library['path']}")
            else:
                print("CONFIGURED Liberty: MISSING (none configured)")
            expected = "AVAILABLE" if report["real_yosys_opensta_flow_expected_runnable"] else "MISSING"
            print(f"Real Yosys/OpenSTA prerequisites: {expected}")
            print("Mock-only operation: AVAILABLE")
            print(report["disclaimer"])
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
