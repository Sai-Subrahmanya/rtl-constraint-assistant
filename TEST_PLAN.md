# Test Plan — RCA Constraint Validation Engine (Step 13)

This test plan describes the validation-engine test coverage. Tests are
kept in `tests/unit/test_validation.py` (Step 7) and
`tests/unit/test_validation_step13.py` (Step 13, 40 named scenarios).

## Suites

| Suite | Location | Count | Notes |
|---|---|---|---|
| Step-7 validation | `tests/unit/test_validation.py` | 74 | Deterministic IDs, clocks, gclks, IO, groups, conflicts, coverage, exceptions, scenarios, backend, immutability. |
| Step-13 validation | `tests/unit/test_validation_step13.py` | 40 | Strengthened reference/semantic/conflict/completeness/exception-safety/scenario/SDC-import/backend/provenance/determinism. |
| Step-12 MCMM | `tests/unit/test_mcmm.py` | 70 | MCMM aggregation, per-scenario identity, cache, evaluator. |
| Step-11 Pareto | `tests/unit/test_pareto.py` | 125 | Multi-objective Pareto/scalar/final selection. |

## Step-13 named scenarios

1. `test_13_01` — valid references produce no false invalid findings.
2. `test_13_02` — nonexistent port flagged when design available (RESOLVED miss).
3. `test_13_03` — design unavailable ⇒ reference is UNRESOLVED, not invalid.
4. `test_13_04` — ref-kind inconsistent (input_delay on a pin).
5. `test_13_05` — ref-kind consistent (input_delay on a port).
6. `test_13_06` — empty selector disallowed for I/O.
7. `test_13_07` — negative design-rule value rejected.
8. `test_13_08` — non-integer fanout rejected.
9. `test_13_09` — clock uncertainty requires a target clock.
10. `test_13_10` — driving cell requires target + cell.
11. `test_13_11` — nonsensical min_delay rejected.
12. `test_13_12` — incompatible divide_by+multiply_by rejected.
13. `test_13_13` — conflicting clock periods flagged.
14. `test_13_14` — conflicting IO delays flagged.
15. `test_13_15` — contradictory exceptions (false_path + multicycle).
16. `test_13_16` — user-vs-inference conflict precedence.
17. `test_13_17` — broad false_path flagged.
18. `test_13_18` — duplicate clock is overlap, not conflict.
19. `test_13_19` — overlapping false paths reported.
20. `test_13_20` — coverage unknown without graph.
21. `test_13_21` — completeness: missing clock period.
22. `test_13_22` — completeness: missing IO timing.
23. `test_13_23` — completeness: unresolved clock relationship.
24. `test_13_24` — completeness: generated clock without transform.
25. `test_13_25` — fixed constraint never modified.
26. `test_13_26` — provenance preserved in issue dict.
27. `test_13_27` — nonexistent scenario id flagged.
28. `test_13_28` — scenario-specific issues preserve identity.
29. `test_13_29` — empty scenario_ids ⇒ all active.
30. `test_13_30` — MCMM active scenarios respected.
31. `test_13_31` — SDC import complete (no incomplete finding).
32. `test_13_32` — SDC import syntax-invalid classified + SYNTAX_ERROR.
33. `test_13_33` — SDC import empty ⇒ incomplete.
34. `test_13_34` — unknown backend flagged without crashing.
35. `test_13_35` — exception unverified when no formal backend.
36. `test_13_36` — deterministic repeated validation.
37. `test_13_37` — severity/blocking ordering.
38. `test_13_38` — report exposes new completeness summary.
39. `test_13_39` — UNKNOWN/UNRESOLVED in `to_dict`.
40. `test_13_40` — issue_id stable with provenance fields.

## Gate criteria

- `tests/unit/test_validation.py` : **74 passed** (no weakened assertions).
- `tests/unit/test_validation_step13.py` : **40 passed**.
- `tests/unit/test_pareto.py` : **125 passed** (Step-11 regression).
- `tests/unit/test_mcmm.py` : **70 passed** (Step-12 regression).
- Full `python -m pytest -q` : **800 collected, 800 passed**.

## Environment notes

The `pyslang` Verilog/SystemVerilog front-end is optional; tests that
exercise the real RTL parser require it and are skipped/reported honestly
in environments without it. This suite does not depend on it.
