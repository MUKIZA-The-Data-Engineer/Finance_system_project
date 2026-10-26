"""The parser interface every bank layout implements.

A parser owns exactly one statement layout. It answers two questions:

    can_parse(path)  "Is this file my layout?"  Cheap, and never raises.
    parse(path)      "Give me its rows."        Emits RawTransaction only.

Parsers do not clean, convert or validate values. Dates and amounts leave a
parser as the text that was printed, so every layout hands the transform layer
the same RawTransaction shape. Adding a bank is one new parser class and one
line in the registry; nothing downstream changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from reconpipe.schemas import RawTransaction


class ParseError(Exception):
    """A file claimed by a parser could not be read as that layout."""

    def __init__(self, path: Path, message: str) -> None:
        super().__init__(f"{path.name}: {message}")
        self.path = path


class BaseStatementParser(ABC):
    """One bank statement layout."""

    #: Short, unique name used in logs and run summaries.
    name: ClassVar[str]

    @abstractmethod
    def can_parse(self, path: Path) -> bool:
        """Return True if the file is this parser's layout.

        Must be cheap (read only as much as needed to recognise the layout)
        and must return False rather than raise for unreadable files.
        """

    @abstractmethod
    def parse(self, path: Path) -> list[RawTransaction]:
        """Read every transaction line in the file, in statement order.

        Raises ParseError if the file looks like this layout but cannot be read.
        """
