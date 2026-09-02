"""
Pydantic configuration model (Enhancement: Pydantic-based validation).

Validates and normalises project YAML files into a strongly-typed
``ProjectConfig`` object consumed by every downstream subsystem.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..utils.enums import (
    FlowStage,
    Priority,
    SafeMode,
)
from ..utils.logging import get_logger
from ..utils.units import parse_frequency_string, parse_time_string
from .schema import PROJECT_SCHEMA, SCHEMA_VERSION

log = get_logger("config")


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class ProjectInfo(BaseModel):
    name: str
    top: str | None = None
    description: str | None = None


class SourceConfig(BaseModel):
    files: list[str] = Field(default_factory=list)
    include_dirs: list[str] = Field(default_factory=list)
    defines: list[str] = Field(default_factory=list)


class UserClockSpec(BaseModel):
    name: str
    period: str | None = None       # e.g. "10ns"
    frequency: str | None = None    # e.g. "100MHz"
    waveform: list[float] | None = None
    port: str | None = None
    fixed: bool = True
    uncertainty: str | None = None

    def period_seconds(self) -> float | None:
        if self.period is not None:
            return parse_time_string(self.period)
        if self.frequency is not None:
            return 1.0 / parse_frequency_string(self.frequency)
        return None


class IOPortSpec(BaseModel):
    delay: str | None = None
    clock: str | None = None
    fixed: bool = True

    def delay_seconds(self) -> float | None:
        return parse_time_string(self.delay) if self.delay else None


class UserIOConfig(BaseModel):
    inputs: dict[str, IOPortSpec] = Field(default_factory=dict)
    outputs: dict[str, IOPortSpec] = Field(default_factory=dict)


class ClockRelationshipSpec(BaseModel):
    clocks: list[str]
    relationship: str  # synchronous|related|asynchronous
    fixed: bool = True


class UserConstraintConfig(BaseModel):
    clocks: list[UserClockSpec] = Field(default_factory=list)
    generated_clocks: list[dict[str, Any]] = Field(default_factory=list)
    io: UserIOConfig = Field(default_factory=UserIOConfig)
    relationships: list[ClockRelationshipSpec] = Field(default_factory=list)
    exceptions: list[dict[str, Any]] = Field(default_factory=list)
    design_rules: dict[str, str] = Field(default_factory=dict)


class ConstraintConfig(BaseModel):
    user: UserConstraintConfig = Field(default_factory=UserConstraintConfig)
    existing_sdc: list[str] = Field(default_factory=list)


class AnalysisConfig(BaseModel):
    language: str = "systemverilog"
    top: str | None = None
    safe_mode: SafeMode = SafeMode.BALANCED


class OptimizationThresholds(BaseModel):
    wns_ps: float = 1.0
    area_pct: float = 0.1
    power_pct: float = 0.1


class OptimizationPerturbation(BaseModel):
    uncertainty_range_ns: list[float] | None = None
    io_delay_range_ns: list[float] | None = None


class OptimizationConfig(BaseModel):
    enabled: bool = False
    max_iterations: int = 20
    max_eda_runs: int = 20
    max_runtime_minutes: int = 120
    convergence_patience: int = 5
    required_setup_margin_ns: float = 0.0
    required_hold_margin_ns: float = 0.0
    priorities: dict[str, Priority] = Field(
        default_factory=lambda: {
            "timing": Priority.HIGH,
            "area": Priority.MEDIUM,
            "power": Priority.MEDIUM,
            "timing_margin_utilization": Priority.MEDIUM,
            "runtime": Priority.LOW,
        }
    )
    perturbation: OptimizationPerturbation = Field(default_factory=OptimizationPerturbation)
    thresholds: OptimizationThresholds = Field(default_factory=OptimizationThresholds)


class PowerReportConfig(BaseModel):
    """One explicitly configured OpenROAD/OpenSTA power-report input.

    The report is an external, tool-produced input.  RCA parses it only after
    a real flow completes; configuration never asks RCA to estimate power.
    """

    model_config = ConfigDict(extra="forbid")

    format: Literal["openroad_report_power"]
    path: str = Field(min_length=1)
    scenario_id: str | None = None
    producer: Literal["openroad_opensta"] = "openroad_opensta"
    producer_version: str | None = None

    @field_validator("path", "producer")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("power report path and producer must not be blank")
        return value

    @field_validator("scenario_id")
    @classmethod
    def _scenario_id_nonblank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("flow.power_reports[].scenario_id must not be blank")
        return value


class FlowConfig(BaseModel):
    backend: str = "generic"
    stage: str = "synthesis_sta"
    liberty: str | list[str] | None = None
    output_dir: str = "output"
    power_reports: list[PowerReportConfig] = Field(default_factory=list)

    def flow_stage(self) -> FlowStage:
        mapping = {
            "rtl_synthesis": FlowStage.RTL_SYNTHESIS,
            "pre_layout_sta": FlowStage.PRE_LAYOUT_STA,
            "post_placement_sta": FlowStage.POST_PLACEMENT_STA,
            "post_route_sta": FlowStage.POST_ROUTE_STA,
            "signoff_sta": FlowStage.SIGNOFF_STA,
            "synthesis_sta": FlowStage.PRE_LAYOUT_STA,  # treat as generic pre-layout
        }
        return mapping.get(self.stage, FlowStage.PRE_LAYOUT_STA)

    def liberty_files(self) -> list[str]:
        if self.liberty is None:
            return []
        if isinstance(self.liberty, str):
            return [self.liberty]
        return list(self.liberty)


class ScenarioSpec(BaseModel):
    id: str | None = None
    mode: str = "functional"
    corner: str = "slow"
    libraries: list[str] = Field(default_factory=list)
    parasitics: str | None = None
    sdc_set_id: str | None = None
    environment: dict[str, Any] = Field(default_factory=dict)
    active: bool = True
    constraints: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reject_nonempty_constraints(self) -> ScenarioSpec:
        """Do not silently accept scenario-specific constraint definitions.

        ``scenarios[].constraints`` is a reserved, currently-unsupported
        configuration block.  Rather than accept it and have no effect, we
        reject non-empty content with a clear error.  Scenario-specific
        constraints must instead be expressed through ``Constraint.scenario_ids``
        on the UCM (Step 12 §2, §12).  This keeps the field from appearing
        functional while doing nothing.
        """
        if self.constraints:
            raise ValueError(
                "scenarios[].constraints is not supported yet; express "
                "scenario-specific constraints through Constraint.scenario_ids "
                f"on the UCM instead (scenario id={self.id or '?'})."
            )
        return self


class MCMMConfig(BaseModel):
    """MCMM (Multi-Mode / Multi-Corner) configuration (Step 12 §12).

    ``enabled`` toggles MCMM evaluation; ``active_scenario_ids`` selects which
    scenarios (from ``scenarios``) are evaluated.  An empty
    ``active_scenario_ids`` means all ``active`` scenarios are evaluated.  When
    disabled or only one scenario is active, behaviour collapses to the legacy
    single-scenario path.
    """

    enabled: bool = False
    active_scenario_ids: list[str] = Field(default_factory=list)


class FormalProofSpec(BaseModel):
    """Explicit, user-authored SymbiYosys proof mapped to one UCM exception.

    RCA deliberately requires the constraint ID and exception kind rather than
    guessing which generated property could establish timing intent.  The SBY
    file contains the design-specific assumptions and assertion(s).
    """

    constraint_id: str = Field(min_length=1)
    exception_kind: Literal["false_path", "multicycle"]
    sby_file: str = Field(min_length=1)
    task: str | None = None

    @field_validator("task")
    @classmethod
    def _validate_task(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or normalized.startswith("-"):
            raise ValueError("formal.proofs[].task must be a non-option task name")
        return normalized


class FormalConfig(BaseModel):
    """Optional formal-exception verification configuration (Step 14).

    The default remains the conservative backend, preserving the historical
    behavior where no real proof is available and exceptions stay UNRESOLVED.
    """

    backend: Literal["conservative", "symbiyosys"] = "conservative"
    symbiyosys_executable: str | None = None
    work_dir: str = "output/formal"
    timeout_seconds: int = Field(default=300, ge=1)
    proofs: list[FormalProofSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_constraint_ids(self) -> FormalConfig:
        ids = [proof.constraint_id for proof in self.proofs]
        duplicates = sorted({cid for cid in ids if ids.count(cid) > 1})
        if duplicates:
            raise ValueError(
                "formal.proofs must contain at most one mapping per constraint_id: "
                + ", ".join(duplicates)
            )
        return self


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------

class ProjectConfig(BaseModel):
    schema_version: str = SCHEMA_VERSION
    project: ProjectInfo
    sources: SourceConfig = Field(default_factory=SourceConfig)
    parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)
    constraints: ConstraintConfig = Field(default_factory=ConstraintConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    flow: FlowConfig = Field(default_factory=FlowConfig)
    optimization: OptimizationConfig = Field(default_factory=OptimizationConfig)
    scenarios: list[ScenarioSpec] = Field(default_factory=list)
    mcmm: MCMMConfig = Field(default_factory=MCMMConfig)
    formal: FormalConfig = Field(default_factory=FormalConfig)

    # Resolved (not from YAML directly)
    config_path: Path | None = Field(default=None, exclude=True)
    project_root: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _resolve_top(self) -> ProjectConfig:
        # Allow top to come from either project.top or analysis.top
        if self.project.top is None and self.analysis.top is not None:
            self.project.top = self.analysis.top
        return self

    @model_validator(mode="after")
    def _validate_power_report_mappings(self) -> ProjectConfig:
        """Reject ambiguous or unsafe scenario-to-report associations.

        A report's scenario binding is configuration provenance.  In MCMM,
        allowing an unlabeled/global report would risk reusing one scenario's
        evidence for another, so it is intentionally forbidden.
        """
        reports = list(self.flow.power_reports)
        scenario_specs = {spec.id: spec for spec in self.scenarios if spec.id}
        all_ids = set(scenario_specs)
        active_ids = set(self.mcmm.active_scenario_ids) if self.mcmm.active_scenario_ids else {
            sid for sid, spec in scenario_specs.items() if spec.active
        }
        unknown_active = active_ids - all_ids
        if self.mcmm.enabled and unknown_active:
            raise ValueError(
                "mcmm.active_scenario_ids contains unknown scenario(s): "
                + ", ".join(sorted(unknown_active))
            )
        if (not self.mcmm.enabled and any(r.scenario_id is None for r in reports)
                and any(r.scenario_id is not None for r in reports)):
            raise ValueError(
                "A single-scenario default power report cannot coexist with a "
                "scenario-labelled power report; the association would be ambiguous."
            )
        seen: set[str] = set()
        for report in reports:
            sid = report.scenario_id
            if self.mcmm.enabled and sid is None:
                raise ValueError(
                    "flow.power_reports[].scenario_id is required when mcmm.enabled=true; "
                    "global power-report fallback is not allowed."
                )
            if sid is None:
                key = "__single_scenario_default__"
            else:
                if sid not in all_ids:
                    raise ValueError(
                        f"flow.power_reports scenario_id '{sid}' is not a configured scenario"
                    )
                if sid not in active_ids:
                    raise ValueError(
                        f"flow.power_reports scenario_id '{sid}' is inactive for this run"
                    )
                key = sid
            if key in seen:
                raise ValueError(
                    f"Duplicate flow.power_reports mapping for scenario "
                    f"'{sid if sid is not None else 'single-scenario default'}'"
                )
            seen.add(key)
        return self

    @field_validator("schema_version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if v != SCHEMA_VERSION:
            log.warning("Config schema_version=%s differs from current %s", v, SCHEMA_VERSION)
        return v

    # --- Resolution helpers ------------------------------------------------

    def top_module(self) -> str:
        top = self.project.top or self.analysis.top
        if not top:
            raise ValueError("Top module must be specified in project.top or analysis.top")
        return top

    def resolve_paths(self) -> None:
        """Resolve all source/include/liberty/sdc paths against project_root."""
        if self.project_root is None:
            return
        root = self.project_root
        self.sources.files = [str((root / f).resolve()) for f in self.sources.files]
        self.sources.include_dirs = [str((root / d).resolve()) for d in self.sources.include_dirs]
        self.constraints.existing_sdc = [
            str((root / f).resolve()) for f in self.constraints.existing_sdc
        ]
        resolved_libs = [str((root / f).resolve()) for f in self.flow.liberty_files()]
        # Keep as list[str] (serialiser-friendly); liberty_files() handles both str|list|None.
        self.flow.liberty = resolved_libs
        out = self.flow.output_dir
        self.flow.output_dir = str((root / out).resolve()) if not Path(out).is_absolute() else out
        for report in self.flow.power_reports:
            report_path = Path(report.path)
            if not report_path.is_absolute():
                report.path = str((root / report_path).resolve())
        formal_work_dir = self.formal.work_dir
        self.formal.work_dir = (
            str((root / formal_work_dir).resolve())
            if not Path(formal_work_dir).is_absolute()
            else formal_work_dir
        )
        for proof in self.formal.proofs:
            proof_path = Path(proof.sby_file)
            if not proof_path.is_absolute():
                proof.sby_file = str((root / proof_path).resolve())

    def output_dir(self) -> Path:
        p = Path(self.flow.output_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_config(path: str | Path) -> ProjectConfig:
    """Load, validate, and return a ProjectConfig from a YAML file."""
    import jsonschema

    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Project config not found: {p}")

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    # JSON Schema validation (for strict schema conformance)
    try:
        jsonschema.validate(raw, PROJECT_SCHEMA)
    except jsonschema.ValidationError as e:
        raise ValueError(f"Invalid project config at {p}: {e.message}") from e

    cfg = ProjectConfig.model_validate(raw)
    cfg.config_path = p
    cfg.project_root = p.parent
    cfg.resolve_paths()

    # Validate clock specs: cannot provide both period and frequency inconsistently
    for clk in cfg.constraints.user.clocks:
        if clk.period and clk.frequency:
            p_s = parse_time_string(clk.period)
            f_s = 1.0 / parse_frequency_string(clk.frequency)
            if abs(p_s - f_s) / max(p_s, 1e-18) > 1e-6:
                raise ValueError(
                    f"Clock '{clk.name}': period '{clk.period}' and frequency "
                    f"'{clk.frequency}' are inconsistent"
                )

    log.info("Loaded project config: %s (top=%s)", cfg.project.name, cfg.project.top)
    return cfg


def default_config(name: str = "new_project", top: str | None = None) -> ProjectConfig:
    """Return a minimal default configuration for ``rca init``."""
    return ProjectConfig(
        project=ProjectInfo(name=name, top=top),
        sources=SourceConfig(),
    )


def write_config(cfg: ProjectConfig, path: str | Path) -> None:
    """Serialise ``cfg`` to YAML at ``path``."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Use model_dump but strip internal path fields
    data = cfg.model_dump(exclude={"config_path", "project_root"})
    p.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False), encoding="utf-8")
