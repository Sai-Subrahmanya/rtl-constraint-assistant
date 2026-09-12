"""Semantic equivalence / comparison package (Step 9 — WP-L)."""

from .normalize import (
    SEMANTIC_FIELDS,
    field_level_diff,
    has_unsupported_options,
    normalize_constraint,
    semantic_match_key,
    constraint_signature_set,
)
from .semantic_compare import (
    ComparisonResult,
    ComparisonLevel,
    ConstraintPairStatus,
    DiffEntry,
    DuplicateRecord,
    FieldDifference,
    PairResult,
    ScenarioDifference,
    compare,
    compare_sdc_text,
)

__all__ = [
    # normalization
    "normalize_constraint", "semantic_match_key", "constraint_signature_set",
    "field_level_diff", "has_unsupported_options", "SEMANTIC_FIELDS",
    # comparison
    "compare", "compare_sdc_text",
    "ComparisonResult", "ComparisonLevel", "ConstraintPairStatus",
    "DiffEntry", "DuplicateRecord", "FieldDifference", "PairResult",
    "ScenarioDifference",
]
