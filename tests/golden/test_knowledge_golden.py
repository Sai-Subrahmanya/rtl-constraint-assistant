"""Golden coverage for deterministic Step-26 built-in knowledge data."""

from __future__ import annotations

import json
from pathlib import Path

from rca.search import KnowledgeEngine


def test_builtin_knowledge_patterns_match_golden_fixture():
    fixture = Path(__file__).with_name("knowledge") / "builtin_patterns.json"
    expected = json.loads(fixture.read_text(encoding="utf-8"))
    actual = {"schema_version": 1, "items": [item.to_dict() for item in KnowledgeEngine().patterns()]}
    assert actual == expected
