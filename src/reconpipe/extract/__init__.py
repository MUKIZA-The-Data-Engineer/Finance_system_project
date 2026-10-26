"""Extract layer: one parser per bank layout, all emitting RawTransaction."""

from reconpipe.extract.bank_a_pdf import BankAPdfParser
from reconpipe.extract.bank_b_pdf import BankBPdfParser
from reconpipe.extract.base import BaseStatementParser, ParseError
from reconpipe.extract.excel_parser import ExcelStatementParser
from reconpipe.extract.registry import (
    AmbiguousParserError,
    ExtractResult,
    ParserRegistry,
    SkippedFile,
    UnsupportedFileError,
    default_registry,
)

__all__ = [
    "AmbiguousParserError",
    "BankAPdfParser",
    "BankBPdfParser",
    "BaseStatementParser",
    "ExcelStatementParser",
    "ExtractResult",
    "ParseError",
    "ParserRegistry",
    "SkippedFile",
    "UnsupportedFileError",
    "default_registry",
]
