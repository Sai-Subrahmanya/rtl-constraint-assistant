# References

This project's architecture was informed by the following industry and
research resources:

## Industry / tooling

1. **Synopsys Timing Constraints Manager** — SDC generation, verification, management, promotion, demotion, equivalency, and formal-oriented correctness checking.
   https://www.synopsys.com/verification/static-and-formal-verification/timing-constraints-manager.html
2. **Synopsys TCM white paper** — Automated Constraint Management for Faster Designer Productivity.
   https://www.synopsys.com/verification/resources/whitepapers/tcm-sdc-management-wp.html
3. **Cadence Conformal Constraint Designer** — SDC validation/refinement, overlap/conflict checks, formal false-path/multicycle validation, hierarchical constraint checks, multi-mode checking, CDC-aware analysis.
   https://www.cadence.com/en_US/home/resources/datasheets/encounter-conformal-constraint-designer-ds.html
4. **OpenSTA** — open-source gate-level static timing verifier (SDC, Liberty, SDF, SPEF, embeddable engine).
   https://github.com/The-OpenROAD-Project/OpenSTA
5. **OpenROAD Flow Scripts** — open-source RTL-to-GDS flow demonstrating Verilog/SDC integration into placement/routing.
   https://openroad-flow-scripts.readthedocs.io/en/latest/tutorials/FlowTutorial.html
6. **slang** — SystemVerilog compiler / parsing / elaboration library used via pyslang.
   https://www.sv-lang.com/
7. **Surelog / UHDM** — alternative SystemVerilog 2017 parser/elaborator (adapter hook provided).
   https://github.com/chipsalliance/Surelog
8. **Yosys** — open-source Verilog synthesis framework.
   https://yosyshq.readthedocs.io/projects/yosys/en/v0.49/cmd/read_verilog.html

## Research

9. *Toward Effective Utilization of Timing Exceptions in Design Optimization* — motivates the exception-effectiveness subsystem (§48): more exceptions ≠ better QoR.
   https://vlsicad.ucsd.edu/Publications/Conferences/268/c268.pdf
