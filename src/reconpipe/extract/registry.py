"""Dispatch a file to the one parser that recognises its layout.

    registry = default_registry()
    rows = registry.parse(Path("data/raw/bank_c_statement_2026Q1.xlsx"))

The caller never names a parser. Each registered parser is asked can_parse();
exactly one must say yes. Zero means an unknown layout, and more than one
means two parsers' recognition rules overlap. Both are errors, because guessing
which parser to trust is how rows get silently misread.
"""

from __future__ import annotations

from pathlib import Path

from reconpipe.extract.base import BaseStatementParser
from reconpipe.extract.excel_parser import ExcelStatementParser
from reconpipe.schemas import RawTransaction


class UnsupportedFileError(Exception):
    """No registered parser recognises the file."""


class AmbiguousParserError(Exception):
    """More than one registered parser claims the file."""


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


def default_registry() -> ParserRegistry:
    """Every bank layout ReconPipe knows. A new bank is one more line here."""
    return ParserRegistry([ExcelStatementParser()])
