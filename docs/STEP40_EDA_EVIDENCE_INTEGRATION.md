# Step 40 — Real EDA Evidence Integration

RCA extends its existing vendor-neutral EDA boundary: bounded preflight
verifies configured Yosys/OpenSTA inputs, Liberty, output location and tool
version before a real flow may start. `rca doctor` is observational; it does
not execute synthesis, STA or formal. `run-sta --backend mock` remains an
explicitly labeled mock path and cannot be recast as a commercial or real-tool
result.

The existing flow preserves typed preflight state, argv-only subprocess
records, manifest/artifact hashes, cache integrity invalidation, tool identity,
scenario, backend, execution mode and failure classification. Missing or
broken tools produce blocked/unavailable outcomes rather than fabricated QoR,
power, reports or signoff. Actual real execution is only stated when its real
manifest evidence exists.

Manifest/artifact integrity remains filesystem evidence authority; SQLite is a
historical query projection only.
