"""
Elaboration helpers.

In the current architecture the pyslang adapter performs elaboration
directly. This subpackage is reserved for future passes that operate on
the normalized Design model — hierarchy resolution, parameter binding,
generate-block unrolling, etc. — independent of any specific parser.
"""
