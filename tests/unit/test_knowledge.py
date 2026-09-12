"""Step-26 typed knowledge/reuse engine tests."""

from __future__ import annotations

import copy
import json

import pytest

from rca.artifacts import RunManifest
from rca.constraint_model import ConstraintSet
from rca.explanation import explain_constraint
from rca.inference.engine import InferenceReport
from rca.qor.model import QoRResult
from rca.qor.repository import SQLiteQoRRepository
from rca.search import (
    ApplicabilityStatus,
    KnowledgeEngine,
    KnowledgeError,
    KnowledgeOrigin,
    TrustLevel,
    accept_suggestion,
    builtin_patterns,
    load_knowledge_file,
)
from rca.utils.enums import ConstraintStatus, SourceKind


def _input_delay(delay: float = 2e-9):
    cset = ConstraintSet(name="source")
    constraint = cset.create_input_delay(
        "din", "clk", delay, source_kind=SourceKind.INFERENCE,
        status=ConstraintStatus.PROPOSED,
    )
    return cset, constraint


def _knowledge_document(constraint, item_id: str = "user-input-delay"):
    return {
        "schema_version": 1,
        "patterns": [{
            "id": item_id,
            "title": "Known input delay",
            "description": "Use an explicit interface delay on din relative to clk.",
            "constraint_template": constraint.to_canonical_dict(),
            "applicability": {
                "required_objects": ["din"],
                "required_clocks": ["clk"],
                "required_constraint_types": ["set_input_delay"],
                "rationale": "The interface and clock must be present.",
            },
            "assumptions": ["interface_budget_approved"],
            "warnings": ["Confirm board timing before emission."],
        }],
    }


def test_builtin_patterns_are_typed_stable_and_not_acceptable_constraints():
    first = [item.to_dict() for item in builtin_patterns()]
    second = [item.to_dict() for item in builtin_patterns()]
    assert first == second
    assert {item["id"] for item in first} >= {
        "K-BUILTIN-CLOCK-DEFINITION", "K-BUILTIN-IO-DELAY",
        "K-BUILTIN-FALSE-PATH", "K-BUILTIN-MULTICYCLE",
    }
    assert {item["trust_level"] for item in first} == {"VALIDATED"}
    assert all(item["constraint_template"] is None for item in first)
    assert all(item["provenance"]["evidence"] for item in first)


def test_project_index_and_semantic_match_use_existing_normalization_not_raw_values():
    source, _known = _input_delay(2e-9)
    engine = KnowledgeEngine(include_builtins=False)
    indexed = engine.index_constraint_set(source)
    assert len(indexed) == 1
    assert indexed[0].origin == KnowledgeOrigin.PROJECT_UCM
    assert indexed[0].trust_level == TrustLevel.UNVERIFIED
    assert indexed[0].ucm_semantic_digest
    assert indexed[0].semantic_digest

    # The equivalent time spelling is intentionally different source text/data.
    query_set, query = _input_delay("2000ps")
    result = engine.search(constraint=query)
    assert result.suggestions[0].match_kind == "semantic_exact"
    assert result.suggestions[0].relevance == 100
    assert result.suggestions[0].applicability == ApplicabilityStatus.APPLICABLE
    assert source.to_snapshot_dict() != query_set.to_snapshot_dict()

    builtins = KnowledgeEngine()
    guidance = builtins.search(constraint=query)
    assert guidance.suggestions[0].match_kind == "canonical_pattern"
    assert guidance.suggestions[0].applicability == ApplicabilityStatus.INSUFFICIENT_EVIDENCE
    assert guidance.suggestions[0].candidate_constraint is None


def test_ranking_is_deterministic_and_same_scope_disagreement_is_not_equivalence():
    first_source, _ = _input_delay(2e-9)
    different_source, _ = _input_delay(3e-9)
    engine = KnowledgeEngine(include_builtins=False)
    engine.index_constraint_set(first_source)
    # Use a different semantic hash to retain both otherwise equal source IDs.
    engine.index_constraint_set(different_source)
    _, query = _input_delay(2e-9)
    first = engine.search(constraint=query).to_dict()
    second = engine.search(constraint=query).to_dict()
    assert first == second
    assert [s["match_kind"] for s in first["suggestions"]] == [
        "semantic_exact", "same_scope_different_values",
    ]
    assert first["suggestions"][0]["relevance"] > first["suggestions"][1]["relevance"]
    assert "disagreement" in " ".join(first["suggestions"][1]["rationale"]).lower()


def test_strong_knowledge_can_be_referenced_by_inference_without_changing_its_safeguards():
    source, _ = _input_delay(2e-9)
    target, query = _input_delay(2e-9)
    engine = KnowledgeEngine(include_builtins=False)
    engine.index_constraint_set(source, origin=KnowledgeOrigin.HISTORY, trust_level=TrustLevel.VALIDATED)
    report = InferenceReport()
    references = engine.inference_references(report, target)
    assert references == report.knowledge_references
    assert references[0]["constraint_id"] == query.id
    assert references[0]["trust_level"] == "VALIDATED"
    assert report.constraints_added == 0 and report.conflicts == [] and report.warnings == []


def test_explicit_acceptance_preserves_fixed_intent_reports_conflict_and_never_overwrites():
    source, known = _input_delay(2e-9)
    engine = KnowledgeEngine(include_builtins=False)
    engine.index_constraint_set(source)

    target, fixed = _input_delay(1e-9)
    fixed.status = ConstraintStatus.FIXED
    fixed.opt_status = "FIXED"
    suggestion = engine.search(constraint=known).suggestions[0]
    accepted = accept_suggestion(suggestion, target)
    assert accepted.status == "ACCEPTED_WITH_CONFLICTS"
    assert accepted.conflict_ids == (fixed.id,)
    assert any(issue["code"] == "CONFLICT_IO_DELAY" for issue in accepted.validation_issues)
    assert fixed.id in target.constraints and target.get(fixed.id).status == ConstraintStatus.FIXED
    added = target.get(accepted.constraint_id)
    assert added is not None and added.status == ConstraintStatus.REQUIRES_CONFIRMATION
    evidence = [ev for ev in added.provenance.evidence if ev.rule_id == "KNOWLEDGE-REUSE"]
    assert len(evidence) == 1
    assert evidence[0].detail["knowledge_item_id"] == suggestion.knowledge_item_id
    assert evidence[0].detail["acceptance_state"] == "EXPLICIT_ACCEPTED_REQUIRES_CONFIRMATION"
    assert suggestion.knowledge_item_id in explain_constraint(added)
    assert any("validation/conflict" in warning.lower() for warning in accepted.warnings)


def test_duplicate_acceptance_is_a_no_op_and_missing_source_links_are_rejected():
    source, known = _input_delay(2e-9)
    engine = KnowledgeEngine(include_builtins=False)
    engine.index_constraint_set(source)
    target, same = _input_delay(2e-9)
    duplicate = accept_suggestion(engine.search(constraint=known).suggestions[0], target)
    assert duplicate.status == "DUPLICATE"
    assert duplicate.duplicate_of == same.id
    assert len(target) == 1

    bad_template = copy.deepcopy(engine.search(constraint=known).suggestions[0])
    candidate = copy.deepcopy(bad_template.candidate_constraint)
    candidate["dependency_ids"] = ["missing-upstream"]
    from dataclasses import replace
    rejected = accept_suggestion(replace(bad_template, candidate_constraint=candidate), ConstraintSet(name="empty"))
    assert rejected.status == "REJECTED"
    assert "missing dependency IDs" in rejected.warnings[0]


def test_user_knowledge_file_is_strict_json_data_only_and_cannot_self_promote(tmp_path):
    _, constraint = _input_delay()
    good = tmp_path / "knowledge.json"
    good.write_text(json.dumps(_knowledge_document(constraint)), encoding="utf-8")
    loaded = load_knowledge_file(good)
    assert loaded[0].origin == KnowledgeOrigin.USER_FILE
    assert loaded[0].trust_level == TrustLevel.USER_PROVIDED
    assert loaded[0].provenance.evidence[-1].kind == "user"

    compact = _knowledge_document(constraint, item_id="compact-user-item")
    compact["patterns"][0]["constraint_template"].pop("provenance")
    compact_path = tmp_path / "compact.json"
    compact_path.write_text(json.dumps(compact), encoding="utf-8")
    assert [item.to_dict() for item in load_knowledge_file(compact_path)] == [
        item.to_dict() for item in load_knowledge_file(compact_path)
    ]

    unknown = _knowledge_document(constraint)
    unknown["patterns"][0]["execute"] = "__import__('os').system('touch should_not_exist')"
    bad = tmp_path / "unknown.json"
    bad.write_text(json.dumps(unknown), encoding="utf-8")
    with pytest.raises(KnowledgeError, match="unsupported field"):
        load_knowledge_file(bad)
    assert not (tmp_path / "should_not_exist").exists()

    yaml_path = tmp_path / "knowledge.yaml"
    yaml_path.write_text("!!python/object/apply:os.system ['touch nope']\n", encoding="utf-8")
    with pytest.raises(KnowledgeError, match="JSON data"):
        load_knowledge_file(yaml_path)
    assert not (tmp_path / "nope").exists()

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version": 1, "schema_version": 1, "patterns": []}', encoding="utf-8")
    with pytest.raises(KnowledgeError, match="duplicate JSON key"):
        load_knowledge_file(duplicate)

    symlink = tmp_path / "linked.json"
    symlink.symlink_to(good)
    with pytest.raises(KnowledgeError, match="must not be a symlink"):
        load_knowledge_file(symlink)


def test_history_projection_is_read_only_and_never_promotes_to_verified(tmp_path):
    snapshot = tmp_path / "source.ucm.json"
    source, _ = _input_delay()
    source.metadata["snapshot_artifact"] = str(snapshot)
    snapshot.write_text(json.dumps(source.to_snapshot_dict()), encoding="utf-8")
    repo = SQLiteQoRRepository.for_output_dir(tmp_path / "output")
    qor = QoRResult(run_id="history-run", setup_wns=1e-9, hold_wns=1e-9, validation_errors=0)
    manifest = RunManifest(candidate_id="baseline", tool="mock", tool_version="1", flow_stage="sta")
    repo.record_flow_evaluation(qor, manifest, run_dir=tmp_path, run_status="SUCCESS", constraint_set=source)
    before = snapshot.read_bytes()

    engine = KnowledgeEngine(include_builtins=False)
    items = engine.index_history(repo)
    assert items and all(item.origin == KnowledgeOrigin.HISTORY for item in items)
    assert all(item.trust_level == TrustLevel.VALIDATED for item in items)
    assert all(item.trust_level != TrustLevel.VERIFIED for item in items)
    assert all(any(ev.rule_id == "KNOWLEDGE-HISTORY" for ev in item.provenance.evidence) for item in items)
    assert snapshot.read_bytes() == before
    projection = repo.list_constraint_set_projections()
    assert projection[0]["run_count"] == 1
    assert projection[0]["validated_run_count"] == 1


def test_history_without_canonical_snapshot_is_diagnostic_not_guessed(tmp_path):
    absent = SQLiteQoRRepository.for_output_dir(tmp_path / "absent-output")
    empty_engine = KnowledgeEngine(include_builtins=False)
    assert empty_engine.index_history(absent) == []
    assert not absent.db_path.exists()
    assert "was not created or modified" in empty_engine.diagnostics[0]

    repo = SQLiteQoRRepository.for_output_dir(tmp_path / "output")
    source, _ = _input_delay()
    repo.record_flow_evaluation(QoRResult(run_id="run"), RunManifest(), run_dir=tmp_path,
                                run_status="SUCCESS", constraint_set=source)
    engine = KnowledgeEngine(include_builtins=False)
    assert engine.index_history(repo) == []
    assert "no canonical UCM snapshot locator" in engine.diagnostics[0]
