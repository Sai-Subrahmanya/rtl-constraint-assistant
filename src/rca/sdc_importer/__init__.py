"""Hardened SDC importer (Step 5).

Three stages:
  A. Lex/Tcl tokenization            (lexer.py)
  B. SDC command + option parsing     (parser.py)
  C. Semantic normalization into UCM  (normalizer.py)

See README_SDC_IMPORTER.md for the supported Tcl subset and security model.
"""

from .collections import (
    DesignResolver,
    TargetCollection,
)
from .lexer import LexError, LexToken, TclLexer
from .normalizer import (
    ImportedConstraint,
    SdcImporter,
    SdcImportResult,
)
from .parser import (
    ParseDiagnostic,
    ParsedSdc,
    SdcCommand,
    SdcOption,
    SdcParser,
    SdcParseResult,
)

__all__ = [
    "LexToken", "TclLexer", "LexError",
    "SdcCommand", "SdcOption", "ParsedSdc", "SdcParseResult", "SdcParser", "ParseDiagnostic",
    "TargetCollection", "DesignResolver",
    "SdcImportResult", "SdcImporter", "ImportedConstraint",
]
