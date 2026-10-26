"""Dispatch a file to the one parser that recognises its layout.

    registry = default_registry()
    rows = registry.parse(Path("data/raw/bank_c_statement_2026Q1.xlsx"))

The caller never names a parser. Each registered parser is asked can_parse();
exactly one must say yes. Zero means an unknown layout, and more than one
means two parsers' recognition rules overlap. Both are errors, because guessing
which parser to trust is how rows get silently misread.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from reconpipe.extract.bank_a_pdf import BankAPdfParser
from reconpipe.extract.bank_b_pdf import BankBPdfParser
from reconpipe.extract.base import BaseStatementParser, ParseError
from reconpipe.extract.excel_parser import ExcelStatementParser
from reconpipe.schemas import RawTransaction


class UnsupportedFileError(Exception):
    """No registered parser recognises the file."""


class AmbiguousParserError(Exception):
    """More than one registered parser claims the file."""


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkippedFile:
    file: str
    reason: str


@dataclass
class ExtractResult:
    """Everything one pass over a folder produced, including what it could not read."""

    transactions: list[RawTransaction] = field(default_factory=list)
    parsed: dict[str, str] = field(default_factory=dict)  # file name -> parser name
    skipped: list[SkippedFile] = field(default_factory=list)


class ParserRegistry:
    def __init__(self, parsers: list[BaseStatementParser] | None = None) -> None:
        self._parsers: list[BaseStatementParser] = []
        for parser in parsers or []:
            self.register(parser)

    def register(self, parser: BaseStatementParser) -> None:
        if any(p.name == parser.name for p in self._parsers):
            raise ValueError(f"a parser named {parser.name!r} is already registered")
        self._parsers.append(parser)

    @property
    def parsers(self) -> tuple[BaseStatementParser, ...]:
        return tuple(self._parsers)

    def parser_for(self, path: Path) -> BaseStatementParser:
        path = Path(path)
        claims = [p for p in self._parsers if p.can_parse(path)]
        if not claims:
            raise UnsupportedFileError(f"{path.name}: no registered parser recognises this file")
        if len(claims) > 1:
            names = ", ".join(p.name for p in claims)
            raise AmbiguousParserError(f"{path.name}: claimed by several parsers ({names})")
        return claims[0]

    def parse(self, path: Path) -> list[RawTransaction]:
        path = Path(path)
        return self.parser_for(path).parse(path)

    def parse_directory(self, directory: Path) -> ExtractResult:
        """Parse every file in a folder into one combined list.

        A file nobody recognises, or one that fails to parse, is logged and
        recorded in ``skipped``; it never stops the other files from loading.
        Hidden files (.gitkeep) are ignored.
        """
        result = ExtractResult()
        paths = sorted(p for p in Path(directory).iterdir() if p.is_file())
        for path in (p for p in paths if not p.name.startswith(".")):
            try:
                parser = self.parser_for(path)
                rows = parser.parse(path)
            except (UnsupportedFileError, AmbiguousParserError, ParseError) as exc:
                logger.warning("skipped %s", exc)  # the message starts with the file name
                result.skipped.append(SkippedFile(path.name, str(exc)))
                continue
            logger.info("parsed %s with %s: %d rows", path.name, parser.name, len(rows))
            result.parsed[path.name] = parser.name
            result.transactions.extend(rows)
        return result


def default_registry() -> ParserRegistry:
    """Every bank layout ReconPipe knows. A new bank is one more line here."""
    return ParserRegistry([BankAPdfParser(), BankBPdfParser(), ExcelStatementParser()])
