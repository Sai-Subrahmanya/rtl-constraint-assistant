# Inference Engine

Hardened evidence-driven inference pipeline (Step 4).

## Architecture

```
Design + TimingGraph + ProjectConfig
         ↓
   InferenceEngine.run()
         ↓
   [Rules in precedence order: USER → structural → heuristic → relationships → candidates]
         ↓  (no rule mutates the UCM)
   list[InferenceResult]
         ↓
   _materialize: merge duplicates, attach provenance, respect precedence
         ↓
   ConstraintSet + InferenceReport (missing info / conflicts / warnings)
```

## Precedence

    USER / EXISTING_SDC (FIXED)
        >
    USER / EXISTING_SDC (CONFIRMED)
        >
    strong structural inference (HIGH confidence)
        >
    weak heuristic / naming hints (LOW/MEDIUM)

When user data contradicts inference, the user value wins; inference
evidence is preserved on the constraint and a conflict entry is added
to the report (no silent overwrite).

## What RCA never fabricates

- Clock periods.
- Input / output delays (no "20% of clock period" fallback).
- Clock relationships (not "asynchronous because different names",
  not "synchronous because same period").
- False paths / multicycle exceptions.
- Generated-clock divide factor / master alignment.
- Clock-gating enable timing.
- Clock-mux exclusivity.

These are either (1) supplied explicitly (USER/EXISTING_SDC, HIGH,
FIXED), (2) determined unambiguously from structural evidence with
HIGH/MEDIUM confidence, or (3) reported as structured
`MissingInformation` with a `RequirementLevel` (REQUIRED /
RECOMMENDED / OPTIONAL / UNSAFE_TO_INFER).

## Missing information

Every missing item carries:

- `id` (stable, e.g. `REQ-CLK-PERIOD-clk`)
- `category` (e.g. `clock_period`, `io_input_delay`,
  `clock_relationship`, `generated_clock_intent`)
- `object`
- `severity`, `requirement_level`, `blocking`
- `message`, `rationale`
- `evidence` (list)
- `suggested_inputs` (what to provide)
- `possible_values` (for ambiguities, e.g. candidate clocks)
- `rule_id`

`blocking=True` means the constraint(s) that depend on this item
cannot be emitted yet; independent inferences continue.

## I/O clock resolution

There is **no default-clock fallback**. Resolution order:

1. Explicit user `clock:` field.
2. TimingGraph's `input_clock_assoc` / `output_clock_assoc`
   (structural fanout/fanin over the connectivity graph).
3. Multiple candidate domains → `REQUIRED` ambiguity, nothing emitted.
4. Zero domains → `REQUIRED` missing clock association, nothing emitted.

## Rule IDs

- CLK-001 structural sequential clock candidate (HIGH)
- CLK-002 clock naming hints (LOW, corroborating only)
- CLK-003 user clock specification (HIGH, USER)
- RST-001 async reset from sensitivity (HIGH)
- RST-002 sync reset candidate (MEDIUM, confirmation)
- RST-003 adversarial reset-name-as-data (MEDIUM, warning)
- REL-001 clock-relationship status (HIGH for USER edges)
- IO-001  I/O port classification (HIGH)
- IO-002  missing input delay (HIGH; produces missing info, no defaults)
- IO-003  missing output delay (HIGH; produces missing info, no defaults)
- GCLK-001 possible clock divider (LOW, candidate only)
- GCLK-002 possible gated clock (LOW, candidate only)
- GCLK-003 possible clock mux (LOW, candidate only)
