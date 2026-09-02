"""
Core enumerations and type definitions for the RCA system.

These enums establish the controlled vocabularies used across the
Universal Constraint Model, design model, validation, and optimization
subsystems. See Project Manual sections 8, 9, 80, 86, 88, 89.
"""

from __future__ import annotations

from enum import Enum, auto


# ---------------------------------------------------------------------------
# Constraint / source / provenance
# ---------------------------------------------------------------------------

class ConstraintType(str, Enum):
    """Supported constraint concept types (Manual §23)."""
    CREATE_CLOCK = "create_clock"
    CREATE_GENERATED_CLOCK = "create_generated_clock"
    SET_CLOCK_UNCERTAINTY = "set_clock_uncertainty"
    SET_CLOCK_LATENCY = "set_clock_latency"
    SET_PROPAGATED_CLOCK = "set_propagated_clock"
    SET_INPUT_DELAY = "set_input_delay"
    SET_OUTPUT_DELAY = "set_output_delay"
    SET_DRIVING_CELL = "set_driving_cell"
    SET_INPUT_TRANSITION = "set_input_transition"
    SET_LOAD = "set_load"
    SET_FALSE_PATH = "set_false_path"
    SET_MULTICYCLE_PATH = "set_multicycle_path"
    SET_MIN_DELAY = "set_min_delay"
    SET_MAX_DELAY = "set_max_delay"
    SET_CLOCK_GROUPS = "set_clock_groups"
    SET_CLOCK_TRANSITION = "set_clock_transition"
    SET_MAX_TRANSITION = "set_max_transition"
    SET_MAX_CAPACITANCE = "set_max_capacitance"
    SET_MAX_FANOUT = "set_max_fanout"
    SET_CASE_ANALYSIS = "set_case_analysis"
    SET_DISABLE_TIMING = "set_disable_timing"


class SourceKind(str, Enum):
    """Origin of a constraint or value (Manual §8.1)."""
    RTL = "RTL"
    USER = "USER"
    EXISTING_SDC = "EXISTING_SDC"
    LIBRARY = "LIBRARY"
    PHYSICAL_DATA = "PHYSICAL_DATA"
    TOOL = "TOOL"
    INFERENCE = "INFERENCE"
    DERIVED = "DERIVED"


class ConstraintStatus(str, Enum):
    """Lifecycle status of a constraint (Manual §8.2)."""
    FIXED = "FIXED"
    CONFIRMED = "CONFIRMED"
    PROPOSED = "PROPOSED"
    REQUIRES_CONFIRMATION = "REQUIRES_CONFIRMATION"
    MISSING = "MISSING"
    REJECTED = "REJECTED"
    DEPRECATED = "DEPRECATED"


class Confidence(str, Enum):
    """Evidence quality classification (Manual §8.3)."""
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class OptimizationStatus(str, Enum):
    """Whether a constraint may be explored by the optimizer (Manual §86)."""
    FIXED = "FIXED"
    TUNABLE = "TUNABLE"
    DERIVED = "DERIVED"
    UNKNOWN = "UNKNOWN"


class GenerationConfidence(str, Enum):
    """Emission confidence category for generated SDC (Manual §88)."""
    DIRECT_FACT = "DIRECT_FACT"
    USER_SPECIFIED = "USER_SPECIFIED"
    DERIVED = "DERIVED"
    INFERRED_HIGH_CONFIDENCE = "INFERRED_HIGH_CONFIDENCE"
    INFERRED_MEDIUM_CONFIDENCE = "INFERRED_MEDIUM_CONFIDENCE"
    PROPOSED_REQUIRES_CONFIRMATION = "PROPOSED_REQUIRES_CONFIRMATION"


class SafeMode(str, Enum):
    """SDC generation safety modes (Step 6 / Manual §89).

    STRICT:      emit only FIXED/HIGH CONFIDENCE constraints.
    BALANCED:    emit FIXED/CONFIRMED + proposals that are safe to
                 render without inventing values.
    AGGRESSIVE:  emit anything that has semantic values (still refuses
                 to bypass missing required fields such as period).
    EXPLORATORY: kept as an alias for AGGRESSIVE for backward compat.
    """
    STRICT = "strict"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"
    EXPLORATORY = "exploratory"  # alias


class OperationMode(str, Enum):
    """Overall tool mode (Manual §90)."""
    ANALYSIS_ONLY = "analysis-only"
    CONSTRAINT_GENERATION = "constraint-generation"
    VALIDATION_ONLY = "validation-only"
    CLOSED_LOOP_OPTIMIZATION = "closed-loop-optimization"


# ---------------------------------------------------------------------------
# Design / timing
# ---------------------------------------------------------------------------

class PortDirection(str, Enum):
    INPUT = "input"
    OUTPUT = "output"
    INOUT = "inout"


class ClockEdge(str, Enum):
    POSEDGE = "posedge"
    NEGEDGE = "negedge"
    BOTH = "both"


class ResetType(str, Enum):
    SYNCHRONOUS = "synchronous"
    ASYNCHRONOUS = "asynchronous"
    UNKNOWN = "unknown"


class ResetPolarity(str, Enum):
    ACTIVE_HIGH = "active_high"
    ACTIVE_LOW = "active_low"
    UNKNOWN = "unknown"


class ClockDomainRelationship(str, Enum):
    """Relationship between two clock domains (Manual §7.6)."""
    SYNCHRONOUS = "synchronous"
    RELATED = "related"
    ASYNCHRONOUS = "asynchronous"
    UNKNOWN = "unknown"


class TimingPathClass(str, Enum):
    """Classifications for logical timing paths (Manual §17)."""
    INPUT_TO_REG = "input_to_register"
    REG_TO_REG = "register_to_register"
    REG_TO_OUTPUT = "register_to_output"
    INPUT_TO_OUTPUT = "input_to_output"
    CLOCK_TO_CONTROL = "clock_to_control"
    RESET_TO_REGISTER = "reset_to_register"
    CDC = "cdc"
    TEST_SCAN = "test_scan"
    GENERATED_CLOCK = "generated_clock"
    COMBINATIONAL = "combinational"


class DependencyKind(str, Enum):
    """Classification for a directed dependency edge in the signal graph.

    Used by the combinational/sequential structural model so that clock and
    reset control signals are kept separate from ordinary data-flow edges.
    """
    DATA = "data"                       # generic data dependency
    CLOCK = "clock"                     # clock control (sensitivity or gating)
    RESET = "reset"                     # asynchronous reset control
    ENABLE = "enable"                   # clock/register enable control
    CONTINUOUS_ASSIGN = "continuous_assign"   # assign w = <expr>
    BLOCKING_ASSIGN = "blocking_assign"       # combinational/seq blocking
    NONBLOCKING_ASSIGN = "nonblocking_assign" # sequential nonblocking (<=)
    CONDITIONAL = "conditional"         # if/condition/select dependency
    MUX_SELECT = "mux_select"           # ternary/case select pin
    CONCATENATION = "concatenation"     # concat/replication membership
    PART_SELECT = "part_select"         # bit/part select dependency
    HIER_PORT_CONN = "hier_port_conn"   # connection across instance boundary
    CLOCK_GATE = "clock_gate"           # clock-enable/gating logic
    UNKNOWN = "unknown"


class UncoveredClassification(str, Enum):
    """Why an object is uncovered (Manual §28, Step 7 §13)."""
    INTENTIONALLY_UNCONSTRAINED = "INTENTIONALLY_UNCONSTRAINED"
    UNCONSTRAINED = "UNCONSTRAINED"
    UNKNOWN = "UNKNOWN"
    REQUIRES_USER_DECISION = "REQUIRES_USER_DECISION"
    REQUIRES_CONFIRMATION = "REQUIRES_CONFIRMATION"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


# ---------------------------------------------------------------------------
# Validation / errors
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    ERROR = "ERROR"
    WARNING = "WARNING"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class RequirementLevel(str, Enum):
    """Classification for missing-information items (Manual §13.3)."""

    REQUIRED = "REQUIRED"
    # Value is necessary to produce a complete, correct timing model;
    # emission of related constraints is blocked until provided.
    RECOMMENDED = "RECOMMENDED"
    # Value improves confidence but missing does not block unrelated
    # inference (e.g. input delay for a data port when the clock is
    # already known).
    OPTIONAL = "OPTIONAL"
    # Nice-to-have (e.g. drive-cell modeling for signoff).
    UNSAFE_TO_INFER = "UNSAFE_TO_INFER"
    # A value is needed and any automatic guess would be high risk
    # (e.g. clock relationship, generated-clock divider factor,
    # exclusivity of a mux). RCA will not fabricate a value; user
    # confirmation is required.


class InferenceResultStatus(str, Enum):
    """Lifecycle state of a rule result."""

    APPLIED = "APPLIED"                 # rule produced concrete constraints that were/are ready
    PROPOSED = "PROPOSED"               # rule produced low/medium confidence proposals needing review
    NO_FINDING = "NO_FINDING"           # rule ran but found nothing applicable
    REQUIRES_CONFIRMATION = "REQUIRES_CONFIRMATION"  # rule found evidence but cannot safely pick
    BLOCKED = "BLOCKED"                 # rule blocked by missing prerequisite information
    UNSAFE_TO_INFER = "UNSAFE_TO_INFER" # rule deliberately declines to guess high-risk intent
    ERROR = "ERROR"                     # rule execution failed



class ErrorCode(str, Enum):
    """Structured error classification (Manual §69, Step 7)."""
    PARSER_ERROR = "PARSER_ERROR"
    ELABORATION_ERROR = "ELABORATION_ERROR"
    MODEL_ERROR = "MODEL_ERROR"
    INFERENCE_WARNING = "INFERENCE_WARNING"
    MISSING_INFORMATION = "MISSING_INFORMATION"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNSAFE_INFERENCE = "UNSAFE_INFERENCE"
    TOOL_ERROR = "TOOL_ERROR"
    REPORT_PARSE_ERROR = "REPORT_PARSE_ERROR"
    OPTIMIZATION_ERROR = "OPTIMIZATION_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"
    COVERAGE_WARNING = "COVERAGE_WARNING"
    CONFLICT_WARNING = "CONFLICT_WARNING"
    # Step 7 issue codes (per-category, deterministically stable).
    MODEL_INVALID = "MODEL_INVALID"
    REF_UNKNOWN = "REF_UNKNOWN"
    REF_UNRESOLVED = "REF_UNRESOLVED"
    REF_UNSUPPORTED_SELECTOR = "REF_UNSUPPORTED_SELECTOR"
    CLOCK_MULTIPLE = "CLOCK_MULTIPLE"
    CLOCK_PERIOD_MISSING = "CLOCK_PERIOD_MISSING"
    CLOCK_PERIOD_INVALID = "CLOCK_PERIOD_INVALID"
    CLOCK_WAVEFORM_INVALID = "CLOCK_WAVEFORM_INVALID"
    CLOCK_WAVEFORM_INCOHERENT = "CLOCK_WAVEFORM_INCOHERENT"
    GCLK_SOURCE_MISSING = "GCLK_SOURCE_MISSING"
    GCLK_MASTER_MISSING = "GCLK_MASTER_MISSING"
    GCLK_TARGET_MISSING = "GCLK_TARGET_MISSING"
    GCLK_INVALID_DIV = "GCLK_INVALID_DIV"
    GCLK_INVALID_MUL = "GCLK_INVALID_MUL"
    GCLK_EDGES_INVALID = "GCLK_EDGES_INVALID"
    GCLK_EDGE_SHIFT_WITHOUT_EDGES = "GCLK_EDGE_SHIFT_WITHOUT_EDGES"
    GCLK_CONTRADICTORY_OPTIONS = "GCLK_CONTRADICTORY_OPTIONS"
    IO_CLOCK_UNKNOWN = "IO_CLOCK_UNKNOWN"
    IO_DELAY_INVALID = "IO_DELAY_INVALID"
    IO_MIN_MAX_INCOHERENT = "IO_MIN_MAX_INCOHERENT"
    IO_WRONG_DIRECTION = "IO_WRONG_DIRECTION"
    IO_DUPLICATE = "IO_DUPLICATE"
    GROUPS_EMPTY = "GROUPS_EMPTY"
    GROUPS_CLOCK_UNKNOWN = "GROUPS_CLOCK_UNKNOWN"
    GROUPS_CONTRADICTORY_RELATIONSHIP = "GROUPS_CONTRADICTORY_RELATIONSHIP"
    GROUPS_DUPLICATE_MEMBER = "GROUPS_DUPLICATE_MEMBER"
    PATH_SELECTOR_EMPTY = "PATH_SELECTOR_EMPTY"
    PATH_SELECTOR_BAD_REF = "PATH_SELECTOR_BAD_REF"
    PATH_SELECTOR_DUPLICATE_STAGE = "PATH_SELECTOR_DUPLICATE_STAGE"
    CONFLICT_CLOCK_PERIOD = "CONFLICT_CLOCK_PERIOD"
    CONFLICT_IO_DELAY = "CONFLICT_IO_DELAY"
    CONFLICT_LATENCY = "CONFLICT_LATENCY"
    CONFLICT_UNCERTAINTY = "CONFLICT_UNCERTAINTY"
    CONFLICT_MINMAX_DELAY = "CONFLICT_MINMAX_DELAY"
    OVERLAP_DUPLICATE = "OVERLAP_DUPLICATE"
    OVERLAP_REDUNDANT = "OVERLAP_REDUNDANT"
    OVERLAP_SHADOWED = "OVERLAP_SHADOWED"
    OVERLAP_OVERLAPPING = "OVERLAP_OVERLAPPING"
    EXCEPTION_BROAD = "EXCEPTION_BROAD"
    EXCEPTION_NO_EFFECT = "EXCEPTION_NO_EFFECT"
    EXCEPTION_SUSPICIOUS = "EXCEPTION_SUSPICIOUS"
    EXCEPTION_BAD_CYCLES = "EXCEPTION_BAD_CYCLES"
    EXCEPTION_SETUP_HOLD_INCOHERENT = "EXCEPTION_SETUP_HOLD_INCOHERENT"
    COVERAGE_CLOCK_GAP = "COVERAGE_CLOCK_GAP"
    COVERAGE_INPUT_GAP = "COVERAGE_INPUT_GAP"
    COVERAGE_OUTPUT_GAP = "COVERAGE_OUTPUT_GAP"
    COVERAGE_REG2REG_GAP = "COVERAGE_REG2REG_GAP"
    COVERAGE_CDC_GAP = "COVERAGE_CDC_GAP"
    COVERAGE_UNKNOWN = "COVERAGE_UNKNOWN"
    SCENARIO_UNKNOWN = "SCENARIO_UNKNOWN"
    SCENARIO_MISMATCH = "SCENARIO_MISMATCH"
    SCENARIO_UNKNOWN_ID = "SCENARIO_UNKNOWN_ID"
    SCENARIO_CONFLICT = "SCENARIO_CONFLICT"
    BACKEND_UNSUPPORTED = "BACKEND_UNSUPPORTED"
    BACKEND_BLOCKED = "BACKEND_BLOCKED"
    SYNTAX_ERROR = "SYNTAX_ERROR"
    # Step 13: reference kind/consistency & unresolved-vs-unknown distinction.
    REF_KIND_INCONSISTENT = "REF_KIND_INCONSISTENT"
    REF_EMPTY_SELECTOR = "REF_EMPTY_SELECTOR"
    REF_UNRESOLVABLE = "REF_UNRESOLVABLE"
    # Step 13: semantic value/unit/range checks.
    CLOCK_UNCERTAINTY_INVALID = "CLOCK_UNCERTAINTY_INVALID"
    CLOCK_LATENCY_INVALID = "CLOCK_LATENCY_INVALID"
    CLOCK_TRANSITION_INVALID = "CLOCK_TRANSITION_INVALID"
    IO_TRANSITION_INVALID = "IO_TRANSITION_INVALID"
    LOAD_INVALID = "LOAD_INVALID"
    DRIVING_CELL_INVALID = "DRIVING_CELL_INVALID"
    DESIGN_RULE_INVALID = "DESIGN_RULE_INVALID"
    MINMAX_DELAY_INVALID = "MINMAX_DELAY_INVALID"
    SEMANTIC_INCOMPATIBLE_OPTION = "SEMANTIC_INCOMPATIBLE_OPTION"
    # Step 13: precedence-aware conflict reporting.
    CONFLICT_PRECEDENCE = "CONFLICT_PRECEDENCE"
    CONFLICT_USER_VS_INFERENCE = "CONFLICT_USER_VS_INFERENCE"
    CONFLICT_EXCEPTION = "CONFLICT_EXCEPTION"
    # Step 13: exception safety.
    EXCEPTION_UNVERIFIED = "EXCEPTION_UNVERIFIED"
    # Step 14: concrete formal-backend outcomes.
    EXCEPTION_FORMAL_INVALID = "EXCEPTION_FORMAL_INVALID"
    EXCEPTION_VERIFICATION_ERROR = "EXCEPTION_VERIFICATION_ERROR"
    # Step 13: completeness / missing-information.
    COMPLETENESS_CLOCK_PERIOD = "COMPLETENESS_CLOCK_PERIOD"
    COMPLETENESS_CLOCK_RELATIONSHIP = "COMPLETENESS_CLOCK_RELATIONSHIP"
    COMPLETENESS_IO_TIMING = "COMPLETENESS_IO_TIMING"
    COMPLETENESS_GENERATED_CLOCK = "COMPLETENESS_GENERATED_CLOCK"
    COMPLETENESS_ENVIRONMENT = "COMPLETENESS_ENVIRONMENT"
    COMPLETENESS_UNRESOLVED = "COMPLETENESS_UNRESOLVED"
    # Step 13: SDC import/parse validation.
    SDC_IMPORT_INCOMPLETE = "SDC_IMPORT_INCOMPLETE"
    SDC_IMPORT_SEMANTIC = "SDC_IMPORT_SEMANTIC"


class ValidationCategory(str, Enum):
    """Step 7 validation phase/category taxonomy."""
    MODEL = "MODEL"
    REFERENCE = "REFERENCE"
    CLOCK = "CLOCK"
    TIMING = "TIMING"
    CONFLICT = "CONFLICT"
    OVERLAP = "OVERLAP"
    COVERAGE = "COVERAGE"
    EXCEPTION = "EXCEPTION"
    SCENARIO = "SCENARIO"
    BACKEND = "BACKEND"
    SYNTAX = "SYNTAX"
    COMPLETENESS = "COMPLETENESS"
    PROVENANCE = "PROVENANCE"


class ValidationStatus(str, Enum):
    """Overall validation verdict (Step 7 §19)."""
    PASS = "PASS"
    PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


class ConstraintPairRelation(str, Enum):
    """Pairwise constraint relationship (Manual §29)."""
    DISJOINT = "disjoint"
    OVERLAPPING = "overlapping"
    IDENTICAL = "identical"
    CONTRADICTORY = "contradictory"
    PRECEDENCE_DEPENDENT = "precedence-dependent"
    SEMANTICALLY_EQUIVALENT = "semantically_equivalent"


class EquivalenceResult(str, Enum):
    """Overall comparison verdict (Step 9 — Work Package L)."""
    EQUIVALENT = "EQUIVALENT"                       # identical after normalization
    EQUIVALENT_AFTER_NORMALIZATION = "EQUIVALENT_AFTER_NORMALIZATION"
    DIFFERENT = "DIFFERENT"                         # semantic difference detected
    PARTIALLY_EQUIVALENT = "PARTIALLY_EQUIVALENT"   # some constraints differ
    NON_EQUIVALENT = "NON_EQUIVALENT"               # retained for back-compat
    UNKNOWN = "UNKNOWN"                             # cannot prove equivalence
    ERROR = "ERROR"                                 # importer / comparison error


class ComparisonLevel(str, Enum):
    """Level at which a pair of constraints match (Step 9 §1)."""
    TEXTUAL = "TEXTUAL"                             # raw text identical
    NORMALIZED = "NORMALIZED"                       # normalized form identical
    SEMANTIC_EQUIVALENT = "SEMANTIC_EQUIVALENT"     # semantically equivalent
    SEMANTIC_DIFFERENT = "SEMANTIC_DIFFERENT"       # semantic difference proven
    UNKNOWN = "UNKNOWN"                             # cannot prove equivalence


class ConstraintPairStatus(str, Enum):
    """Per-pair classification in a comparison report (Step 9 §13)."""
    EQUIVALENT = "EQUIVALENT"
    EQUIVALENT_NORMALIZED = "EQUIVALENT_NORMALIZED"
    DIFFERENT = "DIFFERENT"
    DUPLICATE = "DUPLICATE"             # exact semantic duplicate within one side
    REDUNDANT = "REDUNDANT"             # semantically redundant but harmless
    CONFLICTING = "CONFLICTING"         # same identity but conflicting values
    UNKNOWN = "UNKNOWN"
    ONLY_IN_LEFT = "ONLY_IN_LEFT"
    ONLY_IN_RIGHT = "ONLY_IN_RIGHT"


class DiffAction(str, Enum):
    """Semantic diff actions (Manual §78, extended for Step 9)."""
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    MODIFIED = "MODIFIED"
    SEMANTICALLY_EQUIVALENT = "SEMANTICALLY_EQUIVALENT"
    AFFECTED_PATHS_CHANGED = "AFFECTED_PATHS_CHANGED"
    COVERAGE_CHANGED = "COVERAGE_CHANGED"
    FIELD_DIFFERENCE = "FIELD_DIFFERENCE"
    DUPLICATE = "DUPLICATE"
    REDUNDANT = "REDUNDANT"
    CONFLICTING = "CONFLICTING"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Exception verification
# ---------------------------------------------------------------------------

class VerificationStatus(str, Enum):
    # Step 8 — expanded lifecycle.
    UNCHECKED = "unchecked"                 # not yet analyzed
    PROPOSED = "proposed"                   # proposed, waiting for analysis
    STRUCTURALLY_ANALYZED = "structurally_analyzed"
    VERIFIED = "verified"                   # formal proof / user-approved
    INVALID = "invalid"                     # contradictory / disproved
    UNRESOLVED = "unresolved"               # structural ok but no proof
    ERROR = "error"                         # analysis error
    NOT_APPLICABLE = "not_applicable"       # selector matches nothing structural
    # Legacy alias retained for backward compatibility.
    UNCERTAIN = "unresolved"


class ExceptionRisk(str, Enum):
    """Conservative blast-radius risk classification for exceptions."""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ExceptionApprovalStatus(str, Enum):
    """User-authorization state for a timing exception.

    This is *separate* from VerificationStatus: user approval is an
    authorization signal, NOT a proof of correctness.
    """
    NONE = "NONE"
    USER_CONFIRMED = "USER_CONFIRMED"
    USER_REJECTED = "USER_REJECTED"


class EmissionStatus(str, Enum):
    """Final emission decision after combining verification + approval + risk."""
    ALLOWED = "ALLOWED"
    ALLOWED_USER_CONFIRMED = "ALLOWED_USER_CONFIRMED"
    BLOCKED_INVALID = "BLOCKED_INVALID"
    BLOCKED_UNVERIFIED = "BLOCKED_UNVERIFIED"
    BLOCKED_REJECTED = "BLOCKED_REJECTED"
    BLOCKED_CRITICAL_RISK = "BLOCKED_CRITICAL_RISK"
    BLOCKED_NO_EFFECT = "BLOCKED_NO_EFFECT"
    BLOCKED_ERROR = "BLOCKED_ERROR"


class ExceptionFindingKind(str, Enum):
    """Specific structural findings attached to an exception analysis."""
    BROAD = "BROAD"                                 # no selectors / -from/-to all
    NO_EFFECT = "NO_EFFECT"                         # matches zero structural paths
    CLOCK_DOMAIN_CROSSING = "CLOCK_DOMAIN_CROSSING"
    RESET_RELATED = "RESET_RELATED"
    TEST_MODE = "TEST_MODE"
    USER_INTENT_REQUIRED = "USER_INTENT_REQUIRED"
    REQUIRES_FORMAL_VERIFICATION = "REQUIRES_FORMAL_VERIFICATION"
    CYCLE_COUNT_INVALID = "CYCLE_COUNT_INVALID"
    SETUP_HOLD_INCOHERENT = "SETUP_HOLD_INCOHERENT"
    MULTICYCLE_NO_EVIDENCE = "MULTICYCLE_NO_EVIDENCE"
    CLOCK_GROUP_OVERLAP = "CLOCK_GROUP_OVERLAP"
    UNRESOLVED_SELECTOR = "UNRESOLVED_SELECTOR"


# ---------------------------------------------------------------------------
# Optimization / candidates
# ---------------------------------------------------------------------------

class CandidateDecision(str, Enum):
    """Candidate lifecycle state (Manual §80)."""
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    EDA_PENDING = "EDA_PENDING"
    EVALUATED = "EVALUATED"
    PARETO = "PARETO"
    DOMINATED = "DOMINATED"
    REJECTED_INVALID = "REJECTED_INVALID"
    REJECTED_INFEASIBLE = "REJECTED_INFEASIBLE"
    REJECTED_NO_QOR_GAIN = "REJECTED_NO_QOR_GAIN"
    KEEP = "KEEP"
    FINAL = "FINAL"


class Priority(str, Enum):
    """User-specified objective priority (Manual §43)."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    OFF = "off"


class StopReason(str, Enum):
    """Optimizer termination reasons (Manual §94)."""
    CONVERGED = "converged"
    MAX_ITERATIONS = "max_iterations"
    MAX_EDA_RUNS = "max_eda_runs"
    MAX_TIME = "max_time"
    SEARCH_EXHAUSTED = "search_exhausted"
    ALL_GOALS_SATISFIED = "all_goals_satisfied"
    USER_STOP = "user_stop"
    ERROR = "error"


class FlowStage(str, Enum):
    """EDA flow stages (Manual §56)."""
    RTL_SYNTHESIS = "rtl_synthesis"
    PRE_LAYOUT_STA = "pre_layout_sta"
    POST_PLACEMENT_STA = "post_placement_sta"
    POST_ROUTE_STA = "post_route_sta"
    SIGNOFF_STA = "signoff_sta"


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

class TimeUnit(str, Enum):
    """Internal time is always in seconds; these are user-facing units (Manual §72)."""
    SECOND = "s"
    NANOSECOND = "ns"
    PICOSECOND = "ps"
    FEMTOSECOND = "fs"


class FrequencyUnit(str, Enum):
    HERTZ = "Hz"
    KILOHERTZ = "kHz"
    MEGAHERTZ = "MHz"
    GIGAHERTZ = "GHz"


# ---------------------------------------------------------------------------
# SDC import / collection types
# ---------------------------------------------------------------------------

class CollectionKind(str, Enum):
    """SDC collection types used in [get_* / all_*] commands."""
    PORT = "port"
    PIN = "pin"
    CELL = "cell"
    NET = "net"
    CLOCK = "clock"
    REGISTER = "register"
    ALL_INPUTS = "all_inputs"
    ALL_OUTPUTS = "all_outputs"
    ALL_CLOCKS = "all_clocks"
    ALL_REGISTERS = "all_registers"
    LITERAL = "literal"             # bare name (no [get_*] wrapper)
    UNRESOLVED = "unresolved"       # unknown collection command
    EXPR = "expr"                   # nested / arbitrary expression


class ResolutionStatus(str, Enum):
    RESOLVED = "RESOLVED"           # fully resolved against design
    PATTERN = "PATTERN"             # pattern present, design-aware resolution not attempted or partial
    UNRESOLVED = "UNRESOLVED"       # target could not be resolved (missing or expression)


class ImportStatus(str, Enum):
    """Per-constraint import completeness (Manual §17)."""
    COMPLETE = "COMPLETE"           # all fields understood & resolved
    PARTIAL = "PARTIAL"             # some options preserved but not semantically modeled
    UNRESOLVED = "UNRESOLVED"       # target/option not resolvable yet; original kept
    ERROR = "ERROR"                 # failed to parse; constraint not created


class DiagnosticSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    SECURITY = "SECURITY"           # disallowed construct (exec, source, etc.)


class ClockGroupsRelationship(str, Enum):
    ASYNCHRONOUS = "asynchronous"
    LOGICALLY_EXCLUSIVE = "logically_exclusive"
    PHYSICALLY_EXCLUSIVE = "physically_exclusive"


# ---------------------------------------------------------------------------
# EDA flow status / feasibility (Step 10 — WP-M/N)
# ---------------------------------------------------------------------------


class RunStatus(str, Enum):
    """High-level status of an EDA run."""
    SUCCESS = "SUCCESS"
    BLOCKED = "BLOCKED"           # tool/library missing; never ran
    SYNTHESIS_FAILED = "SYNTHESIS_FAILED"
    STA_FAILED = "STA_FAILED"
    TIMING_FAIL = "TIMING_FAIL"   # ran but setup/hold violations
    CACHE_HIT = "CACHE_HIT"
    MOCK = "MOCK"                 # explicitly mock result (clearly labeled)
    ERROR = "ERROR"


class PowerStatus(str, Enum):
    """Availability and validation state for canonical QoR power evidence.

    Existing ``AVAILABLE``, ``UNAVAILABLE``, and ``ESTIMATED`` values remain
    wire-compatible.  The additional values let report ingestion distinguish
    absent evidence from recognized-but-unknown, malformed, invalid, and
    unsupported evidence without fabricating a numeric power value.
    """

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"   # no configured/reportable power evidence
    ESTIMATED = "ESTIMATED"       # legacy compatibility; parser never assigns it
    UNKNOWN = "UNKNOWN"           # recognized structure but no unique usable total
    MALFORMED = "MALFORMED"       # intended report has syntactically invalid structure/cell
    INVALID = "INVALID"           # parsed values fail semantic validation
    UNSUPPORTED = "UNSUPPORTED"   # file is not the supported report format/unit


class BackendKind(str, Enum):
    REAL_YOSYS_OPENSTA = "yosys_opensta"
    MOCK = "mock"
