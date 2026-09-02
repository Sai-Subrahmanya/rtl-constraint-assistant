"""Hardened SDC importer (Step 5).

Three stages:
  A. Lex/Tcl tokenization            (lexer.py)
  B. SDC command + option parsing     (parser.py)
  C. Semantic normalization into UCM  (normalizer.py)

See README_SDC_IMPORTER.md for the supported Tcl subset and security model.
"""

from .lexer import LexToken, TclLexer, LexError
from .parser import (
    SdcCommand, SdcOption, ParsedSdc, SdcParseResult, SdcParser, ParseDiagnostic,
)
from .collections import (
    TargetCollection, DesignResolver,
)
from .normalizer import (
    SdcImportResult, SdcImporter, ImportedConstraint,
)

__all__ = [
    "LexToken", "TclLexer", "LexError",
    "SdcCommand", "SdcOption", "ParsedSdc", "SdcParseResult", "SdcParser", "ParseDiagnostic",
    "TargetCollection", "DesignResolver",
    "SdcImportResult", "SdcImporter", "ImportedConstraint",
]
