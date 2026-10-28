"""Map whatever a file calls its columns to the standard transaction fields.

    "Date opération" -> date      "Money Out" -> debit      "Libellé" -> description
    "Txn Ref"        -> reference "Solde"     -> balance    "Partner" -> counterparty

Pass 1 matches a whole header exactly against known names (English and French,
accents and punctuation ignored). Pass 2 accepts a header that contains a known
name as whole words ("Debit Amount" -> debit), checking the more specific fields
first so "Debit Amount" is a debit, not an amount. Each field gets at most one
column. Overrides ("Txn Date=date", "Notes=ignore") always win.
"""

from __future__ import annotations

from dataclasses import dataclass

from reconpipe.tools.reader import normalise

FIELDS = (
    "date", "description", "reference", "debit", "credit", "amount", "balance",
    "account", "counterparty", "currency", "record_id",
)  # fmt: skip
IGNORE = "ignore"

SYNONYMS: dict[str, tuple[str, ...]] = {
    "date": (
        "date",
        "transaction date",
        "trans date",
        "txn date",
        "posting date",
        "posted date",
        "value date",
        "booking date",
        "entry date",
        "posted",
        "posting date",
        "date posted",
        "date operation",
        "date d operation",
        "date de l operation",
        "date valeur",
        "date comptable",
        "date transaction",
    ),
    "description": (
        "description",
        "narrative",
        "narration",
        "details",
        "transaction details",
        "particulars",
        "memo",
        "label",
        "remarks",
        "transaction description",
        "libelle",
        "libelle operation",
        "libelle de l operation",
        "designation",
        "motif",
        "communication",
    ),
    "reference": (
        "reference",
        "ref",
        "ref no",
        "ref number",
        "reference number",
        "reference no",
        "txn ref",
        "transaction ref",
        "transaction reference",
        "cheque no",
        "check number",
        "cheque number",
        "document no",
        "doc no",
        "voucher",
        "voucher no",
        "piece",
        "numero de piece",
        "n piece",
        "reference operation",
    ),
    "debit": (
        "debit",
        "debits",
        "debit amount",
        "withdrawal",
        "withdrawals",
        "money out",
        "paid out",
        "payments",
        "dr",
        "out",
        "montant debit",
        "sortie",
        "sorties",
        "retrait",
        "retraits",
    ),
    "credit": (
        "credit",
        "credits",
        "credit amount",
        "deposit",
        "deposits",
        "money in",
        "paid in",
        "receipts",
        "cr",
        "in",
        "montant credit",
        "entree",
        "entrees",
        "versement",
        "versements",
    ),
    "amount": (
        "amount",
        "transaction amount",
        "value",
        "net amount",
        "sum",
        "montant",
        "valeur",
        "amount rwf",
        "amount usd",
    ),
    "balance": (
        "balance",
        "running balance",
        "ledger balance",
        "available balance",
        "solde",
        "solde courant",
    ),
    "account": (
        "account",
        "account number",
        "account no",
        "bank account",
        "a c",
        "a c no",
        "compte",
        "numero de compte",
        "iban",
    ),
    "counterparty": (
        "counterparty",
        "payee",
        "partner",
        "vendor",
        "customer",
        "beneficiary",
        "supplier",
        "client",
        "tiers",
        "party",
        "fournisseur",
        "beneficiaire",
        "partner name",
    ),
    "currency": ("currency", "ccy", "devise", "monnaie"),
    "record_id": (
        "id",
        "entry id",
        "entry",
        "transaction id",
        "txn id",
        "journal entry",
        "line id",
        "record id",
        "entry_id",
    ),
}
# Pass 2 order: specific before general ("Debit Amount" is a debit, not an amount).
CONTAINS_ORDER = (
    "date", "debit", "credit", "balance", "reference", "record_id", "counterparty",
    "currency", "account", "description", "amount",
)  # fmt: skip
REQUIRED = ("date",)


class MappingInputError(ValueError):
    """A --map override that cannot be understood."""


@dataclass(frozen=True)
class ColumnChoice:
    source: str
    field: str  # a FIELD, or IGNORE
    how: str  # exact | contains | override | ai | unmapped


@dataclass
class Mapping:
    choices: list[ColumnChoice]

    @property
    def by_field(self) -> dict[str, str]:
        return {c.field: c.source for c in self.choices if c.field in FIELDS}

    @property
    def missing(self) -> list[str]:
        """Required fields with no column: a date, and some amount column."""
        fields = self.by_field
        gaps = [f for f in REQUIRED if f not in fields]
        if not {"amount", "debit", "credit"} & fields.keys():
            gaps.append("amount (or debit/credit)")
        return gaps


def parse_overrides(pairs: list[str] | None) -> dict[str, str]:
    """['Txn Date=date', 'Notes=ignore'] -> {'Txn Date': 'date', 'Notes': 'ignore'}"""
    out = {}
    for pair in pairs or []:
        source, sep, field = pair.rpartition("=")
        field = field.strip().lower().replace(" ", "_")
        if not sep or not source.strip():
            raise MappingInputError(f"--map expects 'Column name=field', got {pair!r}")
        if field not in (*FIELDS, IGNORE):
            raise MappingInputError(
                f"unknown field {field!r}; use one of: {', '.join(FIELDS)}, ignore"
            )
        out[source.strip()] = field
    return out


def map_columns(header: list[str], overrides: dict[str, str] | None = None) -> Mapping:
    overrides = {normalise(k): v for k, v in (overrides or {}).items()}
    assigned: dict[str, ColumnChoice] = {}
    taken: set[str] = set()

    for col in header:
        field = overrides.get(normalise(col))
        if field:
            assigned[col] = ColumnChoice(col, field, "override")
            if field != IGNORE:
                taken.add(field)

    exact = {syn: f for f, syns in SYNONYMS.items() for syn in syns}
    for col in header:
        if col in assigned:
            continue
        field = exact.get(normalise(col))
        if field and field not in taken:
            assigned[col] = ColumnChoice(col, field, "exact")
            taken.add(field)

    for field in CONTAINS_ORDER:
        if field in taken:
            continue
        for col in header:
            if col in assigned:
                continue
            words = f" {normalise(col)} "
            synonyms = sorted(SYNONYMS[field], key=len, reverse=True)
            if any(f" {s} " in words for s in synonyms if len(s) > 2):
                assigned[col] = ColumnChoice(col, field, "contains")
                taken.add(field)
                break

    return Mapping([assigned.get(c, ColumnChoice(c, IGNORE, "unmapped")) for c in header])
