"""
Artifact management for RCA runs (Manual §57, §58).

Every RCA invocation produces a deterministic output directory containing
JSON / text / SDC artifacts and a ``manifest.json`` that records hashes
of inputs, tool versions, and the configuration used.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..utils.hashing import hash_file, hash_source_set, stable_hash
from ..utils.logging import get_logger

log = get_logger("artifacts")


@dataclass
class RunManifest:
    """Manifest for a single RCA/EDA run (Manual §57)."""
    candidate_id: str = "baseline"
    rtl_hash: dict[str, str] = field(default_factory=dict)
    sdc_hash: str = ""
    config_hash: str = ""
    tool: str = "rca"
    tool_version: str = ""
    flow_stage: str = ""
    mode: str = "functional"
    corner: str = "nominal"
    library: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    artifacts: dict[str, str] = field(default_factory=dict)
    artifact_hashes: dict[str, str] = field(default_factory=dict)
    tool_identity: dict[str, Any] = field(default_factory=dict)
    input_hashes: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "rtl_hash": self.rtl_hash,
            "sdc_hash": self.sdc_hash,
            "config_hash": self.config_hash,
            "tool": self.tool,
            "tool_version": self.tool_version,
            "flow_stage": self.flow_stage,
            "mode": self.mode,
            "corner": self.corner,
            "library": self.library,
            "timestamp": self.timestamp,
            "artifacts": self.artifacts,
            "artifact_hashes": self.artifact_hashes,
            "tool_identity": self.tool_identity,
            "input_hashes": self.input_hashes,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunManifest":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class ArtifactManager:
    """Creates output directories and writes artifacts atomically."""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir = self.output_dir / "runs"
        self.runs_dir.mkdir(exist_ok=True)

    def path(self, *parts: str) -> Path:
        return self.output_dir.joinpath(*parts)

    def write_text(self, rel: str, content: str) -> Path:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        log.debug("Wrote artifact %s (%d bytes)", p, len(content))
        return p

    def write_json(self, rel: str, data: Any) -> Path:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(data, indent=2, sort_keys=False, default=str),
            encoding="utf-8",
        )
        log.debug("Wrote JSON artifact %s", p)
        return p

    def read_json(self, rel: str) -> Any:
        return json.loads(self.path(rel).read_text(encoding="utf-8"))

    def write_manifest(self, manifest: RunManifest) -> Path:
        return self.write_json("manifest.json", manifest.to_dict())

    def candidate_dir(self, candidate_id: str) -> Path:
        d = self.runs_dir / candidate_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def compute_input_hashes(
        self,
        source_files: list[str | Path],
        config_data: Any,
        sdc_path: str | Path | None = None,
    ) -> tuple[dict[str, str], str, str]:
        rtl_hash = hash_source_set(source_files)
        config_hash = stable_hash(config_data)
        sdc_hash = hash_file(sdc_path) if sdc_path and Path(sdc_path).is_file() else ""
        return rtl_hash, sdc_hash, config_hash
