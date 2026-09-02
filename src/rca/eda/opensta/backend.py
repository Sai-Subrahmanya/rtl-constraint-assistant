"""
OpenSTA backend (Step 10 — WP-M).

Supports both the standalone ``sta`` binary and ``openroad -sta`` mode.
Invocation is via argument arrays (NO shell=True). Produces setup/hold
reports, logs, and a Tcl command record in the run manifest.

Liberty requirement (Step 10 §7): if no .lib files are supplied,
``run_sta`` returns a structured BLOCKED result instead of fabricating
timing. If SDC generation status is PARTIAL/BLOCKED and exploratory
policy is not enabled, the run is BLOCKED with a clear reason.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...qor.model import PowerStatus, QoRResult, RunStatus, Feasibility
from ...reports.timing import parse_sta_text
from ...utils.hashing import hash_file, hash_text, stable_hash
from ...utils.logging import get_logger
from ..base import CommandRecord, ToolBackend, ToolInfo

log = get_logger("eda.opensta")


@dataclass
class STAResult:
    qor: QoRResult | None = None
    log_path: Path | None = None
    tcl_path: Path | None = None
    report_paths: dict[str, Path] = field(default_factory=dict)
    tool_info: ToolInfo | None = None
    command: CommandRecord | None = None
    status: str = RunStatus.ERROR.value
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "qor": self.qor.to_dict() if self.qor else None,
            "log": str(self.log_path) if self.log_path else None,
            "tcl": str(self.tcl_path) if self.tcl_path else None,
            "reports": {k: str(v) for k, v in self.report_paths.items()},
            "tool": self.tool_info.to_dict() if self.tool_info else None,
            "command": self.command.to_dict() if self.command else None,
            "error": self.error,
        }


class OpenSTABackend(ToolBackend):
    name = "opensta"
    default_binary_name = "sta"
    env_var = "RCA_OPENSTA"

    def __init__(self, executable: str | None = None,
                 project_local_dirs: list[Path] | None = None) -> None:
        super().__init__(executable=executable, project_local_dirs=project_local_dirs)
        # Also consider openroad -sta as a fallback binary
        if not Path(self.executable).is_file() and not shutil.which(self.executable):
            or_bin = shutil.which("openroad")
            if or_bin:
                self.executable = or_bin
                self._use_openroad = True
            else:
                self._use_openroad = False
        else:
            self._use_openroad = ("openroad" in os.path.basename(self.executable)
                                  if self.executable else False)

    def discover(self) -> ToolInfo:
        info = ToolInfo(vendor=("OpenROAD" if self._use_openroad else "OpenSTA"),
                        tool="opensta", version="unknown",
                        executable=self.executable)
        if not self.executable or not (Path(self.executable).is_file()
                                       or shutil.which(self.executable)):
            info.available = False
            info.error = (f"STA executable not found (tried {self.executable}); "
                          f"set {self.env_var} or eda.opensta_executable")
            return info
        try:
            argv = [self.executable, "-version"]
            rec = self._safe_run(argv, cwd=Path("."), timeout=15)
            out = (rec.stdout_tail or "") + (rec.stderr_tail or "")
            if rec.returncode == 0 and out.strip():
                info.version = out.strip().splitlines()[0]
                info.available = True
            else:
                info.error = f"version probe rc={rec.returncode}: {rec.stderr_tail[:200]}"
        except Exception as e:
            info.error = f"version probe error: {e}"
        info.capabilities = {"sta": True, "sdf": True, "spef": True, "mcmm": True}
        self._info = info
        return info

    def _build_script(self, netlist: Path, sdc: Path, liberty: list[Path],
                      work_dir: Path, top: str, corner: str,
                      ) -> tuple[str, Path, dict[str, Path]]:
        """Build OpenSTA Tcl script; returns (tcl_text, tcl_path, report_paths).

        Hold-validation policy (Step 10 §13): OpenSTA ships ``check_setup``
        for setup/constraint integrity checks only. RCA performs hold
        validation explicitly via ``report_checks -path_delay min`` together
        with ``report_wns -min``/``report_tns -min`` (minimum-analysis WNS
        is hold WNS). This is documented and tested.
        """
        report_base = work_dir / f"{top}.{corner}.sta"
        # Outputs are referenced by basename so the script text is
        # independent of work_dir (run with cwd=work_dir, relative paths resolve).
        setup_rpt = work_dir / f"{top}.{corner}.setup.rpt"
        hold_rpt = work_dir / f"{top}.{corner}.hold.rpt"
        setup_wns = work_dir / f"{top}.{corner}.setup.wns"
        setup_tns = work_dir / f"{top}.{corner}.setup.tns"
        hold_wns = work_dir / f"{top}.{corner}.hold.wns"
        hold_tns = work_dir / f"{top}.{corner}.hold.tns"
        check_rpt = work_dir / f"{top}.{corner}.checks.rpt"
        lines: list[str] = []
        for lib in liberty:
            lines.append(f"read_liberty {lib}")
        lines.append(f"read_verilog {netlist}")
        lines.append(f"link_design {top}")
        lines.append(f"read_sdc {sdc}")
        # Use basenames for redirect targets (script cwd = work_dir).
        lines.append(f"report_checks -path_delay max -format full_clock_expanded "
                     f"-digits 4 -unique_paths_to_endpoint > {setup_rpt.name}")
        lines.append(f"report_checks -path_delay min -format full_clock_expanded "
                     f"-digits 4 -unique_paths_to_endpoint > {hold_rpt.name}")
        lines.append(f"report_wns > {setup_wns.name}")
        lines.append(f"report_tns > {setup_tns.name}")
        lines.append(f"report_wns -min > {hold_wns.name}")
        lines.append(f"report_tns -min > {hold_tns.name}")
        lines.append(f"check_setup -verbose > {check_rpt.name}")
        lines.append("exit")
        tcl_text = "\n".join(lines) + "\n"
        tcl_path = work_dir / "sta.tcl"
        tcl_path.write_text(tcl_text, encoding="utf-8")
        return tcl_text, tcl_path, {
            "setup": setup_rpt, "hold": hold_rpt,
            "setup_wns": setup_wns, "setup_tns": setup_tns,
            "hold_wns": hold_wns, "hold_tns": hold_tns,
            "checks": check_rpt,
        }

    @staticmethod
    def script_semantic_key(netlist: Path, sdc: Path, liberty: list[Path],
                            top: str, corner: str,
                            netlist_hash: str = "", sdc_hash: str = "",
                            lib_hashes: dict[str, str] | None = None,
                            ) -> tuple:
        """Canonical semantic key for the OpenSTA Tcl script/invocation.

        Uses hashes of inputs when provided so that content changes
        (not just path strings) invalidate the cache.
        """
        lib_hashes = lib_hashes or {}
        lib_key = tuple(sorted((str(p), lib_hashes.get(str(p), "")) for p in liberty))
        return (
            "opensta_sta",
            top, corner,
            (str(netlist), netlist_hash),
            (str(sdc), sdc_hash),
            lib_key,
            # command set — fixed in _build_script; change here if script changes
            "read_liberty;read_verilog;link_design;read_sdc;"
            "report_checks(max/min);report_wns/tns(min/max);check_setup;exit",
        )

    def run_sta(self, netlist: Path, sdc: Path, liberty: list[Path],
                work_dir: Path, top: str, corner: str = "default",
                extra_args: dict[str, Any] | None = None,
                prebuilt_script: tuple[str, Path, dict[str, Path]] | None = None,
                ) -> STAResult:
        extra_args = extra_args or {}
        timeout = int(extra_args.get("timeout", 600))
        allow_partial_sdc = bool(extra_args.get("allow_partial_sdc", False))
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        res = STAResult(log_path=work_dir / "sta.log",
                        tcl_path=work_dir / "sta.tcl")

        if not liberty:
            res.status = RunStatus.BLOCKED.value
            res.error = "Liberty (.lib) required for real STA but none supplied."
            return res
        for lib in liberty:
            if not Path(lib).is_file():
                res.status = RunStatus.BLOCKED.value
                res.error = f"Liberty file not found: {lib}"
                return res
        if not Path(netlist).is_file():
            res.status = RunStatus.BLOCKED.value
            res.error = f"Synthesized netlist not found: {netlist}"
            return res
        if not Path(sdc).is_file():
            res.status = RunStatus.BLOCKED.value
            res.error = f"SDC file not found: {sdc}"
            return res
        if not allow_partial_sdc and extra_args.get("sdc_status") in ("PARTIAL", "BLOCKED"):
            res.status = RunStatus.BLOCKED.value
            res.error = (f"Refusing signoff STA: SDC generation status is "
                         f"{extra_args.get('sdc_status')}. Pass allow_partial_sdc=True "
                         f"for exploratory mode.")
            return res

        info = self._info or self.discover()
        res.tool_info = info
        if not info.available:
            res.status = RunStatus.BLOCKED.value
            res.error = info.error
            return res

        if prebuilt_script is not None:
            tcl_text, tcl_path, report_map = prebuilt_script
        else:
            tcl_text, tcl_path, report_map = self._build_script(
                netlist, sdc, liberty, work_dir, top, corner)
        res.tcl_path = tcl_path

        if self._use_openroad:
            argv = [self.executable, "-exit", str(tcl_path)]
        else:
            argv = [self.executable, str(tcl_path)]

        log.info("Running %s on %s", self.executable, netlist)
        rec = self._safe_run(argv, cwd=work_dir, timeout=timeout)
        res.command = rec
        res.log_path.write_text(
            (rec.stdout_tail or "") + "\n" + (rec.stderr_tail or ""),
            encoding="utf-8", errors="replace")

        if rec.returncode != 0:
            res.status = RunStatus.STA_FAILED.value
            res.error = f"sta exited rc={rec.returncode}; see {res.log_path}"
            return res

        chunks: list[str] = []
        for name, p in report_map.items():
            res.report_paths[name] = p
            if p.is_file():
                chunks.append(p.read_text(encoding="utf-8", errors="replace"))
        text = "\n".join(chunks)
        qor = parse_sta_text(text)
        qor.tool = "opensta"
        qor.tool_version = info.version
        qor.raw_report_text = text
        qor.power_status = PowerStatus.UNAVAILABLE.value
        # Store Tcl script hash for audit
        qor.notes = qor.notes or []
        qor.notes.append(f"sta_script_hash={hash_text(tcl_text)}")
        qor.notes.append("hold validated via report_checks -path_delay min + "
                         "report_wns/tns -min (OpenSTA check_setup covers setup only).")
        qor.feasibility = Feasibility.from_qor(qor).to_dict()
        res.qor = qor
        feas = Feasibility.from_qor(qor)
        if feas.blocked:
            res.status = RunStatus.BLOCKED.value
            res.error = feas.reason
        elif not feas.setup_pass or not feas.hold_pass:
            res.status = RunStatus.TIMING_FAIL.value
        else:
            res.status = RunStatus.SUCCESS.value
        return res



