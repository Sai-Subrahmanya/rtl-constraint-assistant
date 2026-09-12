"""Deterministic search capabilities.

``rca.optimizer.search`` remains the optimizer's deterministic exploration
strategy.  This package also provides the Step-26 offline knowledge/reuse
advisory service in :mod:`rca.search.knowledge`; it indexes canonical UCM
patterns without becoming a second constraint model or constraint authority.
"""

from .knowledge import (
    KNOWLEDGE_SCHEMA_VERSION,
    MAX_KNOWLEDGE_FILE_BYTES,
    AcceptanceResult,
    ApplicabilityConditions,
    ApplicabilityStatus,
    KnowledgeEngine,
    KnowledgeError,
    KnowledgeOrigin,
    KnowledgePattern,
    KnowledgeSearchResult,
    KnowledgeSuggestion,
    TrustLevel,
    accept_suggestion,
    builtin_patterns,
    load_knowledge_file,
    pattern_from_constraint,
)

__all__ = [
    "KNOWLEDGE_SCHEMA_VERSION",
    "MAX_KNOWLEDGE_FILE_BYTES",
    "AcceptanceResult",
    "ApplicabilityConditions",
    "ApplicabilityStatus",
    "KnowledgeEngine",
    "KnowledgeError",
    "KnowledgeOrigin",
    "KnowledgePattern",
    "KnowledgeSearchResult",
    "KnowledgeSuggestion",
    "TrustLevel",
    "accept_suggestion",
    "builtin_patterns",
    "load_knowledge_file",
    "pattern_from_constraint",
]
