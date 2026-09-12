# Step 35 — Project Configuration

`ProjectConfig` remains RCA's one project configuration model. Step 35 adds the
optional strict `workflow` block; it does not introduce a parallel config
format or policy authority.

```yaml
workflow:
  ucm_snapshot: reviewed-ucm.json
  knowledge_sources: [knowledge.json]
  inference_policy: {}
  application_policy: {explicit: true}
  validation_policy: {}
  coverage_policy: {}
  readiness_policy: {}
  review_policy: {}
  release_policy: {}
  handoff_policy: {sdc_dialect: OPENSTA}
  release_package_dir: artifacts/release-r1
  handoff_target: OPENSTA_OPENROAD
```

The policy mappings are declarative inputs that their existing owning engines
validate and consume. `workflow.handoff_target` is strictly one of `GENERIC`,
`OPENSTA_OPENROAD`, `SYNOPSYS`, `CADENCE`, or `FUTURE_VENDOR`; unknown fields
and malformed policy mappings fail config validation. YAML remains the normal
format; JSON is YAML-compatible where existing `yaml.safe_load` is used.

`ProjectConfig.engineering_dict()` is a portable deterministic projection used
by release/handoff identities. Paths below the configured project root become
relative, optional nulls are normalized, and load-location-specific absolute
paths do not change engineering identity. `identity_dict()` preserves the
legacy source-evidence shape while omitting an all-default optional workflow
block, so old advisory/readiness/lineage identities remain stable. Runtime path
resolution for actual I/O remains unchanged. `write_config()` omits nulls so
its YAML round-trips through the existing strict schema.
