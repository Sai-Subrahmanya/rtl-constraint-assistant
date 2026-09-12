# Step 45 — Canonical Offline E2E Demonstration

`examples/governed_workflow` is the canonical offline demonstration.

```bash
PYTHONPATH=../../src python demo.py
# or after installation:
python examples/governed_workflow/demo.py
```

It performs read-only governed workflow and replay-evidence projections,
explicitly writes dashboard presentation artifacts, runs the existing EDA flow
only with `--backend mock`, and runs `rca doctor` for bounded real-tool
preflight. It asserts/prints these boundaries:

- no implicit candidate application, review approval, release, handoff, or
  external signoff;
- `EDA_UNAVAILABLE` in the workflow absent actual supplied EDA manifest;
- explicit `MOCK` EDA output, never claimed real;
- real Yosys/OpenSTA/formal are only preflighted and may be unavailable;
- `automatic_replay_supported: false`.

`tests/integration/test_governed_workflow_demo.py` copies and runs this example
without installed EDA tools, checking its artifacts and labels. The demo writes
only its local ignored `output/` directory.
