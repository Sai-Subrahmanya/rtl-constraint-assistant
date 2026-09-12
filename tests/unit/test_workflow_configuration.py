"""Step-35 configuration extension and portable engineering identity tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rca.config import ProjectConfig, WorkflowConfig, load_config, write_config


def _raw(root: Path) -> dict:
    return {
        "schema_version": "1.0", "project": {"name": "workflow", "top": "top"},
        "sources": {"files": ["rtl/top.sv"], "include_dirs": [], "defines": []},
        "workflow": {
            "ucm_snapshot": "reviewed-ucm.json", "knowledge_sources": ["knowledge.json"],
            "inference_policy": {"safe": True}, "application_policy": {"explicit": True},
            "validation_policy": {"backend": "generic"}, "coverage_policy": {"require_complete": True},
            "readiness_policy": {}, "review_policy": {}, "release_policy": {}, "handoff_policy": {},
            "release_package_dir": "artifacts/release", "handoff_target": "OPENSTA_OPENROAD",
        },
    }


def test_workflow_configuration_loads_validates_and_resolves_references(tmp_path: Path):
    import yaml

    path = tmp_path / "project.yaml"; path.write_text(yaml.safe_dump(_raw(tmp_path), sort_keys=False), encoding="utf-8")
    config = load_config(path)
    assert config.workflow.handoff_target == "OPENSTA_OPENROAD"
    assert Path(config.workflow.ucm_snapshot).is_absolute()
    assert Path(config.workflow.knowledge_sources[0]).is_absolute()
    assert Path(config.workflow.release_package_dir).is_absolute()


def test_engineering_config_identity_is_portable_across_project_roots(tmp_path: Path):
    import yaml

    left = tmp_path / "left"; right = tmp_path / "right"; left.mkdir(); right.mkdir()
    for root in (left, right):
        (root / "project.yaml").write_text(yaml.safe_dump(_raw(root), sort_keys=False), encoding="utf-8")
    assert load_config(left / "project.yaml").engineering_dict() == load_config(right / "project.yaml").engineering_dict()


@pytest.mark.parametrize("bad", ["OTHER", "", "opensta"])
def test_invalid_workflow_target_fails_with_clear_validation_error(tmp_path: Path, bad: str):
    import yaml

    data = _raw(tmp_path); data["workflow"]["handoff_target"] = bad
    path = tmp_path / "bad.yaml"; path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="handoff_target"):
        load_config(path)


def test_unknown_workflow_fields_and_nonobject_policies_fail_closed(tmp_path: Path):
    import yaml

    data = _raw(tmp_path); data["workflow"]["unsafe_unknown"] = True
    path = tmp_path / "unknown.yaml"; path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)
    data = _raw(tmp_path); data["workflow"]["release_policy"] = ["not", "a", "mapping"]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_workflow_config_json_yaml_round_trip_is_deterministic(tmp_path: Path):
    config = ProjectConfig.model_validate(_raw(tmp_path))
    assert WorkflowConfig.model_validate(config.workflow.model_dump()).model_dump() == config.workflow.model_dump()
    out = tmp_path / "out.yaml"; write_config(config, out)
    loaded = load_config(out)
    assert json.dumps(loaded.engineering_dict(), sort_keys=True) == json.dumps(config.engineering_dict(), sort_keys=True)
