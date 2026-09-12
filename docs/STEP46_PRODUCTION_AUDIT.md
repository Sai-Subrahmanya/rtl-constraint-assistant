# Step 46 — Production Architecture and Safety Audit

## Authority and transitions

There is one canonical `ConstraintSet`/UCM. Inference and knowledge are
advisory; application is explicit; validation is evidence analysis; review and
release are explicit governance; handoff consumes release evidence; EDA/formal
execution are separate explicit boundaries. SQLite remains historical/query
storage; manifest/artifact hashes remain execution evidence authority.

## Fail-closed evidence

Unknown/missing/ambiguous evidence is surfaced as pending, unresolved, blocked,
review-required, or unavailable. It never becomes a generated period, scenario,
proof, application, approval, release, QoR or signoff. Release does not create
or decide reviews and is not EDA/STA/physical/ASIC signoff. Mock runs are
labelled mock; commercial execution is not claimed without actual evidence.

## Security and portability

Configuration, knowledge and proof configurations are declarative and strictly
validated. User knowledge is bounded data, not Python/Tcl/shell. Subprocess
backends use argv with `shell=False`, bounded execution and retained/redacted
evidence. Report output is constrained to the configured output root and the
dashboard escapes dynamic HTML. Project-root locators, timestamps and machine
paths are excluded from new engineering/replay/formal deterministic identities.

## Verification gate

Run `ruff check src tests`, `pytest`, and the explicit optional real-tool suite
only when its tools/collateral are executable. The canonical offline demo and
its E2E test must pass everywhere. Real-tool unavailability is a valid explicit
outcome; fabricated tool or signoff evidence is not.
