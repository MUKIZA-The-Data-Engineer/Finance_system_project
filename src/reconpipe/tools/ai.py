"""Tool 5 (AI assistant): a prompt library for finance files, answered by Claude.

    reconpipe ai list                               the prompts available
    reconpipe ai show pandas_transform stmt.xlsx    the prompt, filled in (no API key needed)
    reconpipe ai run odoo_mapping stmt.pdf          send it to Claude, save the answer
    reconpipe ai map-columns odd.csv                let Claude say which column is which
    reconpipe standardize odd.csv --ai              ... or only when the rules cannot tell

Prompts live in reconpipe/tools/prompts/*.md: a short front matter block
(title, description) and a body with {placeholders} filled from the file.

What leaves the computer: the column names and the first few rows of the file
(--headers-only sends the column names alone). Nothing is sent by `ai show`.

Column mappings Claude suggests are remembered in config/column_memory.json,
keyed by the file's header, and reused for every later file with the same
columns, with or without --ai and without calling Claude again.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from reconpipe.tools.columns import FIELDS, IGNORE, ColumnChoice, Mapping
from reconpipe.tools.reader import Table, cell_to_text, normalise, read_tables

PROMPT_DIR = Path(__file__).parent / "prompts"
MEMORY = Path("config/column_memory.json")  # or set RECONPIPE_COLUMN_MEMORY
MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
SAMPLE_ROWS = 5
MIN_CONFIDENCE = 0.5

MAPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "columns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "column": {"type": "string"},
                    "field": {"type": "string", "enum": [*FIELDS, IGNORE]},
                    "confidence": {"type": "number"},
                },
                "required": ["column", "field", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["columns"],
    "additionalProperties": False,
}

SYSTEM = (
    "You assist an accountant who reconciles bank statements against the books and "
    "prepares imports for Odoo. Be precise and brief; never invent columns or values "
    "that are not in the data you are shown."
)


class AIError(Exception):
    """Claude could not be reached, or did not answer."""


# --------------------------------------------------------------------------- #
# The prompt library
# --------------------------------------------------------------------------- #


@dataclass
class Prompt:
    name: str
    title: str
    description: str
    body: str

    @property
    def placeholders(self) -> set[str]:
        return set(re.findall(r"\{(\w+)\}", self.body))


def _load(path: Path) -> Prompt:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if match:
        for line in match.group(1).splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
        text = text[match.end() :]
    return Prompt(path.stem, meta.get("title", path.stem), meta.get("description", ""), text)


def prompts() -> dict[str, Prompt]:
    return {p.stem: _load(p) for p in sorted(PROMPT_DIR.glob("*.md"))}


def get_prompt(name: str) -> Prompt:
    library = prompts()
    if name not in library:
        raise AIError(f"no prompt named {name!r}; available: {', '.join(library)}")
    return library[name]


def _text_table(header: list[str], rows: list[list[object]]) -> str:
    lines = [" | ".join(header)]
    lines += [" | ".join(cell_to_text(v) for v in row) for row in rows]
    return "\n".join(lines)


def table_context(table: Table, headers_only: bool = False) -> dict[str, str]:
    """What a prompt may say about one table: its columns and a few sample rows."""
    rows = [r for r in table.rows if any(cell_to_text(v) for v in r)][:SAMPLE_ROWS]
    sample = (
        "(sample rows not shared)"
        if headers_only or not rows
        else f"First rows:\n\n{_text_table(table.header, rows)}"
    )
    return {
        "columns": "\n".join(f"- {c}" for c in table.header),
        "sample_rows": sample,
    }


def file_context(path: Path, headers_only: bool = False, needs: set[str] = frozenset()) -> dict:
    from reconpipe.tools.odoo import JOURNAL_COLUMNS, STATEMENT_COLUMNS

    read = read_tables(Path(path))
    context = {
        "file_name": Path(path).name,
        "file_kind": read.kind,
        "fields": ", ".join(FIELDS),
        "odoo_statement_columns": ", ".join(STATEMENT_COLUMNS),
        "odoo_journal_columns": ", ".join(JOURNAL_COLUMNS),
        **table_context(read.tables[0], headers_only),
    }
    if {"issues", "mapping"} & needs:
        from reconpipe.tools.standardizer import MappingError, standardize_tables

        try:
            result = standardize_tables(read)
        except MappingError as exc:
            context["issues"] = f"- the columns could not be mapped: {exc}"
            context["mapping"] = "(none)"
        else:
            context["issues"] = (
                "\n".join(
                    f"- row {i.row}: {i.problem} ({i.severity}, {i.action})"
                    + ("" if headers_only else f" value: {i.value}")
                    for i in result.issues
                )
                or "(no issues)"
            )
            context["mapping"] = "\n".join(
                f"- {c.source} -> {c.field} ({c.how})"
                for m in result.mappings.values()
                for c in m.choices
            )
    return context


def render(name: str, path: Path, headers_only: bool = False) -> str:
    prompt = get_prompt(name)
    context = file_context(path, headers_only, prompt.placeholders)
    return prompt.body.format_map({k: context.get(k, f"{{{k}}}") for k in prompt.placeholders})


# --------------------------------------------------------------------------- #
# Calling Claude
# --------------------------------------------------------------------------- #


def make_client():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - installed with the package
        raise AIError("the anthropic package is not installed: pip install anthropic") from exc
    try:
        return anthropic.Anthropic()
    except anthropic.AnthropicError as exc:
        raise AIError(
            "no Claude API key found: set ANTHROPIC_API_KEY (or run `ant auth login`)"
        ) from exc


def ask_claude(
    text: str,
    client=None,
    schema: dict | None = None,
    effort: str = "medium",
) -> str:
    """One request to Claude; returns the answer text (JSON text when schema is given).

    Uses server-side fallbacks: if the model declines, the API re-runs the same
    request on Anthropic's recommended fallback model inside the same call.
    """
    import anthropic

    client = client or make_client()
    output_config: dict = {"effort": effort}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": text}],
            output_config=output_config,
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as exc:
        raise AIError("the Claude API key was rejected; check ANTHROPIC_API_KEY") from exc
    except anthropic.RateLimitError as exc:
        raise AIError("Claude is rate limiting this key; try again in a minute") from exc
    except anthropic.APIConnectionError as exc:
        raise AIError("could not reach the Claude API (network or proxy)") from exc
    except anthropic.APIStatusError as exc:
        raise AIError(f"the Claude API returned an error: {exc.message}") from exc
    if response.stop_reason == "refusal":
        raise AIError("Claude declined to answer this request")
    if response.stop_reason == "max_tokens":
        raise AIError("Claude's answer was cut off (too long)")
    answer = "".join(b.text for b in response.content if b.type == "text").strip()
    if not answer:
        raise AIError("Claude returned an empty answer")
    return answer


def run(name: str, path: Path, headers_only: bool = False, client=None, effort="medium") -> str:
    return ask_claude(render(name, path, headers_only), client, effort=effort)


# --------------------------------------------------------------------------- #
# Column mapping: remembered, and asked of Claude only when needed
# --------------------------------------------------------------------------- #


def fingerprint(header: list[str]) -> str:
    joined = "|".join(normalise(h) for h in header)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def memory_file() -> Path:
    return Path(os.environ.get("RECONPIPE_COLUMN_MEMORY", MEMORY))


def load_memory(path: Path | None = None) -> dict:
    path = Path(path or memory_file())
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AIError(f"{path} is not valid JSON: {exc}") from exc


def remember(header: list[str], fields: dict[str, str], path: Path | None = None) -> Path:
    path = Path(path or memory_file())
    memory = load_memory(path)
    memory[fingerprint(header)] = {"columns": header, "fields": fields}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(memory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def suggest_mapping(
    table: Table, client=None, headers_only: bool = False
) -> list[tuple[str, str, float]]:
    """Ask Claude which standard field each column holds: [(column, field, confidence)]."""
    body = get_prompt("map_columns").body
    context = {"fields": ", ".join(FIELDS), **table_context(table, headers_only)}
    answer = ask_claude(body.format_map(context), client, schema=MAPPING_SCHEMA, effort="low")
    try:
        data = json.loads(answer)
    except json.JSONDecodeError as exc:
        raise AIError("Claude's column mapping was not valid JSON") from exc
    known = set(table.header)
    return [
        (c["column"], c["field"], float(c["confidence"]))
        for c in data["columns"]
        if c["column"] in known
    ]


def merge(mapping: Mapping, fields: dict[str, str]) -> Mapping:
    """Fill the gaps in a rule-based mapping; rules and --map overrides keep priority."""
    taken = {c.field for c in mapping.choices if c.field != IGNORE}
    choices = []
    for choice in mapping.choices:
        field = fields.get(choice.source)
        if choice.how == "unmapped" and field and field != IGNORE and field not in taken:
            choice = ColumnChoice(choice.source, field, "ai")
            taken.add(field)
        choices.append(choice)
    return Mapping(choices)


def make_mapper(
    use_ai: bool = False,
    client=None,
    memory_path: Path | None = None,
    headers_only: bool = False,
    on_ask: Callable[[Table], None] | None = None,
) -> Callable[[Table, Mapping], Mapping]:
    """The standardiser's mapper hook: memory first, then Claude when use_ai is set."""

    def mapper(table: Table, mapping: Mapping) -> Mapping:
        known = load_memory(memory_path).get(fingerprint(table.header))
        if known:
            return merge(mapping, known["fields"])
        if not use_ai:
            return mapping
        if on_ask:
            on_ask(table)
        suggestion = suggest_mapping(table, client, headers_only)
        fields = {col: f for col, f, conf in suggestion if conf >= MIN_CONFIDENCE}
        improved = merge(mapping, fields)
        if not improved.missing:
            remember(table.header, {c.source: c.field for c in improved.choices}, memory_path)
        return improved

    return mapper
