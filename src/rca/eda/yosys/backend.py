"""
Yosys synthesis backend (Step 10 — WP-M).

Invokes the Yosys CLI via ``subprocess`` with argument arrays (NO shell).
Produces:
- synthesized gate-level Verilog netlist
- yosys.log (full stdout/stderr)
- synth.ys (the exact script used, for reproducibility)
- synthesis_stats.json (parsed stat output, cell counts, area proxy)
- a CommandRecord for the invocation

Liberty handling:
- If one or more .lib files are supplied, ABC is used to map to the
  technology library. Area from ``stat -liberty`` is then the real
  mapped area.
- If no Liberty is supplied, the backend still produces a generic
  techmapped netlist (useful for bring-up) but marks area as a
  proxy (cell count) and downstream STA is BLOCKED per Step 10 §7.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...utils.hashing import hash_file, hash_text, stable_hash
from ...utils.logging import get_logger
from ..base import CommandRecord, ToolBackend, ToolInfo

log = get_logger("eda.yosys")


_CELL_COUNT_RE = re.compile(r"Number of cells:\s*(\d+)", re.IGNORECASE)
_WIRE_COUNT_RE = re.compile(r"Number of wires:\s*(\d+)", re.IGNORECASE)
_FF_RE = re.compile(r"SB_DFF\S*|\bDFF\S*|\$_DFF_\S+", re.IGNORECASE)
_CHIP_AREA_RE = re.compile(r"Chip area for (?:module|top module) '\S+':\s*([\d.eE+-]+)")
_CHIP_AREA_RE2 = re.compile(r"Chip area[^:]*:\s*(-?\d+\.?\d*(?:[eE][+-]?\d+)?)")


@dataclass
class SynthResult:
    netlist: Path
    log_path: Path
    script_path: Path
    stats_path: Path
    stats: dict[str, Any] = field(default_factory=dict)
    tool_info: ToolInfo | None = None
    command: CommandRecord | None = None
    success: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "netlist": str(self.netlist),
            "log": str(self.log_path),
            "script": str(self.script_path),
            "stats": str(self.stats_path),
            "stats_summary": self.stats,
            "tool": self.tool_info.to_dict() if self.tool_info else None,
            "command": self.command.to_dict() if self.command else None,
            "success": self.success,
            "error": self.error,
        }


class YosysBackend(ToolBackend):
    name = "yosys"
    default_binary_name = "yosys"
    env_var = "RCA_YOSYS"

    def __init__(self, executable: str | None = None,
                 project_local_dirs: list[Path] | None = None) -> None:
        super().__init__(executable=executable, project_local_dirs=project_local_dirs)

    def discover(self) -> ToolInfo:
        info = ToolInfo(vendor="Yosys", tool="yosys", version="unknown",
                        executable=self.executable)
        if not self.executable or not Path(self.executable).is_file() \
                and not shutil.which(self.executable):
            info.available = False
            info.error = f"yosys executable not found (tried {self.executable}); " \
                         f"set {self.env_var} or eda.yosys_executable"
            return info
        try:
            # Yosys prints version to stderr on -V. Safe argv, no shell.
            rec = self._safe_run([self.executable, "-V"], cwd=Path("."), timeout=15)
            if rec.returncode == 0:
                out = (rec.stdout_tail or "") + (rec.stderr_tail or "")
                info.version = out.strip().splitlines()[0] if out else "unknown"
                info.available = True
            else:
                info.error = f"version probe failed rc={rec.returncode}: {rec.stderr_tail}"
        except Exception as e:
            info.error = f"version probe error: {e}"
        info.capabilities = {"synthesis": True, "systemverilog": True, "abc": True}
        self._info = info
        return info

    def _build_script(self, sources: list[Path], top: str, liberty: list[Path],
                      work_dir: Path, defines: dict[str, str] | None = None,
                      include_dirs: list[str] | None = None,
                      parameters: dict[str, str] | None = None
                      ) -> tuple[str, Path, Path]:
        """Build and write the Yosys synthesis script.

        Returns (script_text, script_path, netlist_path). The script text
        uses absolute path references, but a canonical semantic key is
        available via :meth:`script_semantic_key` for cache identity.
        """
        defines = defines or {}
        include_dirs = include_dirs or []
        parameters = parameters or {}
        work_dir.mkdir(parents=True, exist_ok=True)
        netlist = work_dir / f"{top}_synth.v"
        lines: list[str] = []
        for d, val in defines.items():
            if val is None or val == "":
                lines.append(f"verilog_defaults -add -D{d}")
            else:
                lines.append(f"verilog_defaults -add -D{d}={val}")
        for inc in include_dirs:
            lines.append(f"verilog_defaults -add -I{inc}")
        for src in sources:
            lines.append(f"read_verilog -sv {src}")
        if liberty:
            for lib in liberty:
                lines.append(f"read_liberty -lib {lib}")
        chs_args = " ".join(f"-P{k}={v}" for k, v in sorted(parameters.items()))
        lines.append(f"hierarchy -check -top {top} {chs_args}".rstrip())
        lines.append("proc; opt; fsm; opt; memory; opt")
        if liberty:
            lines.append("techmap; opt")
            lines.append(f"dfflibmap -liberty {liberty[0]}")
            lines.append(f"abc -liberty {liberty[0]}")
            lines.append("opt; clean")
            lines.append(f"stat -liberty {liberty[0]}")
        else:
            lines.append("techmap; opt; clean")
            lines.append(f"stat -top {top}")
        # Use basename so script text is independent of work_dir/run_id
        # (yosys is invoked with cwd=work_dir so relative paths resolve).
        lines.append(f"write_verilog -noattr {netlist.name}")
        script = "\n".join(lines) + "\n"
        script_path = work_dir / "synth.ys"
        script_path.write_text(script, encoding="utf-8")
        return script, script_path, netlist

    @staticmethod
    def script_semantic_key(sources: list[Path], top: str, liberty: list[Path],
                            defines: dict[str, str] | None = None,
                            include_dirs: list[str] | None = None,
                            parameters: dict[str, str] | None = None,
                            source_hashes: dict[str, str] | None = None,
                            lib_hashes: dict[str, str] | None = None,
                            ) -> tuple:
        """Canonical semantic identity for the synthesis flow.

        Uses source hashes and lib hashes (not just path strings) where
        supplied so that changing file contents invalidates the cache
        even if the path is unchanged.
        """
        defines = defines or {}
        include_dirs = include_dirs or []
        parameters = parameters or {}
        source_hashes = source_hashes or {}
        lib_hashes = lib_hashes or {}
        # Source identity: sorted by path, hash preferred.
        src_key = tuple(
            sorted((str(p), source_hashes.get(str(p), "")) for p in sources)
        )
        lib_key = tuple(
            sorted((str(p), lib_hashes.get(str(p), "")) for p in liberty)
        )
        return (
            "yosys_synth",
            top,
            tuple(sorted(defines.items())),
            tuple(sorted(include_dirs)),
            tuple(sorted(parameters.items())),
            src_key,
            lib_key,
            "proc;opt;fsm;opt;memory;opt;techmap;dfflibmap;abc;opt;clean;stat;write_verilog",
        )

    def synthesize(self, sources: list[Path], top: str, liberty: list[Path],
                   work_dir: Path, sdc_out: Path | None = None,
                   extra_args: dict[str, Any] | None = None) -> SynthResult:
        extra_args = extra_args or {}
        timeout = int(extra_args.get("timeout", 600))
        defines = extra_args.get("defines", {}) or {}
        include_dirs = [str(p) for p in (extra_args.get("include_dirs") or [])]
        parameters = extra_args.get("parameters", {}) or {}
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        log_path = work_dir / "yosys.log"
        stats_path = work_dir / "synthesis_stats.json"

        script, script_path, netlist = self._build_script(
            sources, top, liberty, work_dir,
            defines=defines, include_dirs=include_dirs, parameters=parameters,
        )

        log.info("Running yosys for top=%s -> %s (bin=%s)", top, netlist, self.executable)
        rec = self._safe_run(
            [self.executable, "-s", str(script_path)],
            cwd=work_dir, timeout=timeout,
        )
        log_path.write_text(
            (rec.stdout_tail or "") + ("\n--- STDERR ---\n" + rec.stderr_tail if rec.stderr_tail else ""),
            encoding="utf-8", errors="replace",
        )

        result = SynthResult(netlist=netlist, log_path=log_path,
                             script_path=script_path, stats_path=stats_path,
                             tool_info=self._info or self.discover(),
                             command=rec)
        if rec.returncode != 0:
            result.error = f"yosys exited rc={rec.returncode}; see {log_path}"
            return result
        if not netlist.is_file():
            result.error = f"yosys did not produce netlist at {netlist}"
            return result

        log_text = (rec.stdout_tail or "") + "\n" + (rec.stderr_tail or "")
        result.stats = _parse_yosys_stat(log_text, bool(liberty))
        result.stats["script_hash"] = hash_text(script)
        stats_path.write_text(json.dumps(result.stats, indent=2, sort_keys=True),
                              encoding="utf-8")
        result.success = True
        return result

    def run_sta(self, netlist, sdc, liberty, work_dir, top, corner="default", extra_args=None):
        raise NotImplementedError("Use OpenSTABackend for STA.")


def _parse_yosys_stat(text: str, has_liberty: bool) -> dict[str, Any]:
    stats: dict[str, Any] = {"cell_count": None, "ff_count": None,
                             "wire_count": None, "area": None,
                             "area_is_proxy": not has_liberty}
    m = _CELL_COUNT_RE.search(text)
    if m:
        stats["cell_count"] = int(m.group(1))
    m = _WIRE_COUNT_RE.search(text)
    if m:
        stats["wire_count"] = int(m.group(1))
    stats["ff_count"] = len(_FF_RE.findall(text))
    m = _CHIP_AREA_RE.search(text) or _CHIP_AREA_RE2.search(text)
    if m:
        try:
            stats["area"] = float(m.group(1))
        except ValueError:
            pass
    if stats["area"] is None and stats["cell_count"] is not None:
        stats["area_proxy"] = stats["cell_count"]
    return stats
