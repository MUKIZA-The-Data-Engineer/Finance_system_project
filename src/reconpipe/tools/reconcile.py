"""Tool 3: reconcile any two sets of files (A against B), e.g. bank statements against the books.

    result = reconcile_files([Path("bank.pdf")], [Path("books.xlsx")])

Both sides go through the standardiser first, so any PDF, Excel or CSV works.
Matching then runs the same cascade as the warehouse pipeline, strictest
evidence first, each tier seeing only what the earlier ones left:

    tier 1  exact reference      lines sharing a reference are summed on both sides,
                                 so one-to-many and many-to-one settle as a group;
                                 same reference but a different total -> amount_mismatch
    tier 2  amount + date        within the amount and day tolerances, and each pair
                                 is the other's closest candidate (no line used twice)
    tier 3  amount + description wider window; similarity = 0.5 x word overlap
                                 + 0.5 x (both sides name the same counterparty)
    tier 4  group amount         one line equals the sum of 2-3 lines on the other
                                 side (same account and sign, within a few days)

Before matching, lines identical to an earlier one on the same side except for
their own ID are reported as duplicate_suspected and kept out of matching.
Whatever is left is missing on the other side.
"""

from __future__ import annotations

import datetime as dt
import itertools
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from reconpipe.load.warehouse import find_counterparty
from reconpipe.tools.standardizer import StandardizeOutput, StandardRow, standardize_file
from reconpipe.transform import clean_description

TIERS = ("tier1_exact", "tier2_amount_date", "tier3_fuzzy", "tier4_group_amount")
STATUSES = ("matched", "amount_mismatch", "missing_in_b", "missing_in_a", "duplicate_suspected")


@dataclass(frozen=True)
class Tolerances:
    amount: Decimal = Decimal("0.00")  # how far apart two amounts may be and still match
    days: int = 2  # tier 2 date window
    description_days: int = 14  # tier 3 date window
    min_similarity: float = 0.4  # tier 3 threshold
    group_days: int = 7  # tier 4 date window
    max_group: int = 3  # tier 4: at most this many lines on the "many" side


DEFAULT_TOLERANCES = Tolerances()


@dataclass
class Item:
    """One reconciliation result: a match, a mismatch, a missing line or a duplicate."""

    status: str
    a: list[StandardRow] = field(default_factory=list)
    b: list[StandardRow] = field(default_factory=list)
    tier: str | None = None
    confidence: float | None = None
    detail: str = ""

    @property
    def a_amount(self) -> Decimal | None:
        return sum((r.amount for r in self.a), Decimal("0")) if self.a else None

    @property
    def b_amount(self) -> Decimal | None:
        return sum((r.amount for r in self.b), Decimal("0")) if self.b else None

    @property
    def difference(self) -> Decimal | None:
        if self.a and self.b:
            return self.b_amount - self.a_amount
        return None


@dataclass
class ReconcileResult:
    items: list[Item]
    a_label: str
    b_label: str
    tolerances: Tolerances
    a_inputs: list[StandardizeOutput] = field(default_factory=list)
    b_inputs: list[StandardizeOutput] = field(default_factory=list)

    def of(self, status: str) -> list[Item]:
        return [i for i in self.items if i.status == status]

    @property
    def summary(self) -> dict:
        a_lines = sum(len(i.a) for i in self.items if i.status != "duplicate_suspected")
        matched = self.of("matched")
        matched_a = sum(len(i.a) for i in matched)
        tiers = Counter()
        for i in matched:
            tiers[i.tier] += len(i.a)
        statuses = Counter(i.status for i in self.items)
        exceptions = [i for i in self.items if i.status != "matched"]
        return {
            "a_label": self.a_label,
            "b_label": self.b_label,
            "a_lines": a_lines,
            "b_lines": sum(len(i.b) for i in self.items),
            "matched_a_lines": matched_a,
            "match_rate": round(matched_a / a_lines, 4) if a_lines else 0.0,
            "matched_a_lines_by_tier": {t: tiers[t] for t in TIERS},
            "items_by_status": {s: statuses[s] for s in STATUSES},
            "value_matched": sum((abs(i.a_amount) for i in matched), Decimal("0")),
            "value_in_exception": sum(
                (abs(i.a_amount if i.a else i.b_amount) for i in exceptions), Decimal("0")
            ),
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _ref_key(ref: str | None) -> str | None:
    key = re.sub(r"\W", "", ref or "").casefold()
    return key or None


def _same_account(a: StandardRow, b: StandardRow) -> bool:
    return a.account_id is None or b.account_id is None or a.account_id == b.account_id


def _close(x: Decimal, y: Decimal, tol: Decimal) -> bool:
    return abs(x - y) <= tol


def _days(a: StandardRow, b: StandardRow) -> int:
    return abs((a.date - b.date).days)


def _words(row: StandardRow) -> set[str]:
    return set(row.description_clean.split())


def _duplicates(rows: list[StandardRow]) -> tuple[list[StandardRow], list[Item]]:
    """Split later copies of identical lines (differing only in their own ID) from the rest."""
    first: dict[tuple, StandardRow] = {}
    keep, dupes = [], []
    for r in rows:
        key = (r.account_id, r.date, r.amount, _ref_key(r.reference), r.description_clean)
        if key in first:
            original = first[key]
            label = original.record_id or f"row {original.row_number}"
            dupes.append(Item("duplicate_suspected", detail=f"identical to {label}", b=[r]))
        else:
            first[key] = r
            keep.append(r)
    return keep, dupes


# --------------------------------------------------------------------------- #
# Tiers
# --------------------------------------------------------------------------- #


def _tier1(a: list[StandardRow], b: list[StandardRow], tol: Tolerances) -> list[Item]:
    groups_a, groups_b = defaultdict(list), defaultdict(list)
    for r in a:
        if _ref_key(r.reference):
            groups_a[(r.account_id, _ref_key(r.reference))].append(r)
    for r in b:
        if _ref_key(r.reference):
            groups_b[(r.account_id, _ref_key(r.reference))].append(r)
    # A side without accounts can still meet a B side with them: match on the reference alone.
    if any(acc is None for acc, _ in groups_a) or any(acc is None for acc, _ in groups_b):
        groups_a = _by_ref(groups_a)
        groups_b = _by_ref(groups_b)
    items = []
    for key in groups_a.keys() & groups_b.keys():
        ga, gb = groups_a[key], groups_b[key]
        total_a = sum((r.amount for r in ga), Decimal("0"))
        total_b = sum((r.amount for r in gb), Decimal("0"))
        shape = f", {len(ga)}:{len(gb)} lines" if len(ga) > 1 or len(gb) > 1 else ""
        ok = _close(total_a, total_b, tol.amount)
        items.append(
            Item(
                "matched" if ok else "amount_mismatch",
                ga,
                gb,
                "tier1_exact",
                1.0,
                f"reference {ga[0].reference}{shape}",
            )
        )
    return items


def _by_ref(groups: dict) -> dict:
    merged = defaultdict(list)
    for (_, ref), rows in groups.items():
        merged[(None, ref)].extend(rows)
    return merged


def _mutual_best(pairs: list[tuple[tuple, StandardRow, StandardRow]]) -> list[tuple]:
    """Repeatedly keep pairs that are both sides' best remaining candidate."""
    chosen, used_a, used_b = [], set(), set()
    pairs = sorted(pairs, key=lambda p: p[0])
    while True:
        best_a, best_b = {}, {}
        for p in pairs:
            _, ra, rb = p
            if id(ra) in used_a or id(rb) in used_b:
                continue
            best_a.setdefault(id(ra), p)
            best_b.setdefault(id(rb), p)
        new = [p for p in best_a.values() if best_b.get(id(p[2])) is p]
        if not new:
            return chosen
        for p in new:
            chosen.append(p)
            used_a.add(id(p[1]))
            used_b.add(id(p[2]))


def _tier2(a: list[StandardRow], b: list[StandardRow], tol: Tolerances) -> list[Item]:
    pairs = []
    for ra in a:
        for rb in b:
            if _same_account(ra, rb) and _close(ra.amount, rb.amount, tol.amount):
                days = _days(ra, rb)
                if days <= tol.days:
                    pairs.append(((abs(ra.amount - rb.amount), days, rb.row_number), ra, rb))
    items = []
    for (diff, days, _), ra, rb in _mutual_best(pairs):
        confidence = round(max(0.0, 1.0 - 0.05 * days - (0.05 if diff else 0.0)), 2)
        detail = (
            f"same amount, {days} day(s) apart"
            if not diff
            else f"amounts {diff} apart, {days} day(s) apart"
        )
        items.append(Item("matched", [ra], [rb], "tier2_amount_date", confidence, detail))
    return items


def _tier3(
    a: list[StandardRow], b: list[StandardRow], tol: Tolerances, a_counterparty: dict[int, str]
) -> list[Item]:
    pairs = []
    for ra in a:
        for rb in b:
            if not (_same_account(ra, rb) and _close(ra.amount, rb.amount, tol.amount)):
                continue
            days = _days(ra, rb)
            if days > tol.description_days:
                continue
            wa, wb = _words(ra), _words(rb)
            overlap = len(wa & wb) / min(len(wa), len(wb)) if wa and wb else 0.0
            cp_b = clean_description(rb.counterparty, None) if rb.counterparty else None
            same_cp = bool(cp_b) and a_counterparty.get(id(ra)) == cp_b
            similarity = round(0.5 * overlap + 0.5 * same_cp, 2)
            if similarity >= tol.min_similarity:
                pairs.append(((-similarity, days, rb.row_number), ra, rb))
    items = []
    for (neg_sim, days, _), ra, rb in _mutual_best(pairs):
        detail = f"same amount, {days} day(s) apart, similarity {-neg_sim}"
        items.append(Item("matched", [ra], [rb], "tier3_fuzzy", -neg_sim, detail))
    return items


def _tier4(a: list[StandardRow], b: list[StandardRow], tol: Tolerances) -> list[Item]:
    items, used = [], set()

    def search(single_side: list[StandardRow], many_side: list[StandardRow], single_is_a: bool):
        for one in single_side:
            if id(one) in used:
                continue
            candidates = [
                r
                for r in many_side
                if id(r) not in used
                and _same_account(one, r)
                and (r.amount > 0) == (one.amount > 0)
                and abs(r.amount) < abs(one.amount)
                and _days(one, r) <= tol.group_days
            ]
            candidates = sorted(candidates, key=lambda r: _days(one, r))[:12]
            for size in range(2, tol.max_group + 1):
                combo = next(
                    (
                        c
                        for c in itertools.combinations(candidates, size)
                        if _close(sum((r.amount for r in c), Decimal("0")), one.amount, tol.amount)
                    ),
                    None,
                )
                if combo:
                    used.add(id(one))
                    used.update(id(r) for r in combo)
                    a_rows, b_rows = ([one], list(combo)) if single_is_a else (list(combo), [one])
                    shape = f"{len(a_rows)}:{len(b_rows)} lines, same total"
                    items.append(Item("matched", a_rows, b_rows, "tier4_group_amount", 0.8, shape))
                    break

    search(a, b, single_is_a=True)
    search(b, a, single_is_a=False)
    return items


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def reconcile_rows(
    a_rows: list[StandardRow],
    b_rows: list[StandardRow],
    tolerances: Tolerances = DEFAULT_TOLERANCES,
    a_label: str = "bank",
    b_label: str = "books",
) -> ReconcileResult:
    a_rows, dupes_a = _duplicates(a_rows)
    for d in dupes_a:  # duplicates found on side A belong to A
        d.a, d.b = d.b, []
    b_rows, dupes_b = _duplicates(b_rows)

    keys = sorted(
        {clean_description(r.counterparty, None) for r in b_rows if r.counterparty} - {""}
    )
    a_counterparty = {
        id(r): (clean_description(r.counterparty, None) if r.counterparty else None)
        or find_counterparty(r.description_clean, keys)
        for r in a_rows
    }

    items: list[Item] = []
    remaining_a, remaining_b = list(a_rows), list(b_rows)

    def take(new: list[Item]) -> None:
        nonlocal remaining_a, remaining_b
        items.extend(new)
        used_a = {id(r) for i in new for r in i.a}
        used_b = {id(r) for i in new for r in i.b}
        remaining_a = [r for r in remaining_a if id(r) not in used_a]
        remaining_b = [r for r in remaining_b if id(r) not in used_b]

    take(_tier1(remaining_a, remaining_b, tolerances))
    take(_tier2(remaining_a, remaining_b, tolerances))
    take(_tier3(remaining_a, remaining_b, tolerances, a_counterparty))
    take(_tier4(remaining_a, remaining_b, tolerances))

    items += [Item("missing_in_b", [r], [], detail=f"not found in {b_label}") for r in remaining_a]
    items += [Item("missing_in_a", [], [r], detail=f"not found in {a_label}") for r in remaining_b]
    items += dupes_a + dupes_b
    order = {s: i for i, s in enumerate(STATUSES)}
    items.sort(key=lambda i: (order[i.status], _first_date(i), _first_row(i)))
    return ReconcileResult(items, a_label, b_label, tolerances)


def _first_date(item: Item) -> dt.date:
    return min(r.date for r in item.a + item.b)


def _first_row(item: Item) -> tuple:
    r = (item.a or item.b)[0]
    return (r.source_file, r.row_number)


def reconcile_files(
    a_files: list[Path],
    b_files: list[Path],
    tolerances: Tolerances = DEFAULT_TOLERANCES,
    a_label: str = "bank",
    b_label: str = "books",
    a_overrides: dict[str, str] | None = None,
    b_overrides: dict[str, str] | None = None,
    date_order: str = "auto",
    today: dt.date | None = None,
    mapper=None,
) -> ReconcileResult:
    a_inputs = [standardize_file(f, a_overrides, date_order, today, mapper) for f in a_files]
    b_inputs = [standardize_file(f, b_overrides, date_order, today, mapper) for f in b_files]
    result = reconcile_rows(
        [r for s in a_inputs for r in s.rows],
        [r for s in b_inputs for r in s.rows],
        tolerances,
        a_label,
        b_label,
    )
    result.a_inputs, result.b_inputs = a_inputs, b_inputs
    return result


# --------------------------------------------------------------------------- #
# Excel report
# --------------------------------------------------------------------------- #


def _side(rows: list[StandardRow]) -> list[object]:
    if not rows:
        return ["", "", "", "", "", "", ""]
    return [
        min(r.date for r in rows),
        sum((r.amount for r in rows), Decimal("0")),
        "; ".join(r.reference for r in rows if r.reference),
        " | ".join(r.description for r in rows),
        "; ".join(r.record_id for r in rows if r.record_id),
        "; ".join(sorted({r.source_file for r in rows})),
        ", ".join(str(r.row_number) for r in rows),
    ]


def report_sheets(result: ReconcileResult):
    from reconpipe.tools.excel_out import Sheet

    A, B = result.a_label.capitalize(), result.b_label.capitalize()
    labels = {
        "matched": "Matched",
        "amount_mismatch": "Amount mismatch",
        "missing_in_b": f"Missing in {result.b_label}",
        "missing_in_a": f"Missing in {result.a_label}",
        "duplicate_suspected": "Duplicate suspected",
    }
    side_cols = ["date", "amount", "reference", "description", "id", "file", "rows"]
    columns = ["Status", "Tier", "Confidence", "Detail", "Account", "Difference"]
    columns += [f"{A} {c}" for c in side_cols] + [f"{B} {c}" for c in side_cols]
    money = (f"{A} amount", f"{B} amount", "Difference")

    def row(i: Item) -> list[object]:
        account = (i.a or i.b)[0].account_id
        return [labels[i.status], i.tier, i.confidence, i.detail, account, i.difference,
                *_side(i.a), *_side(i.b)]  # fmt: skip

    s = result.summary
    t = result.tolerances
    summary = [
        [f"{A} lines", s["a_lines"]],
        [f"{B} lines", s["b_lines"]],
        [f"{A} lines matched", s["matched_a_lines"]],
        ["Match rate", f"{s['match_rate']:.1%}"],
        *[[f"  matched by {k}", v] for k, v in s["matched_a_lines_by_tier"].items()],
        *[[labels[k], v] for k, v in s["items_by_status"].items()],
        ["Value matched", s["value_matched"]],
        ["Value in exception", s["value_in_exception"]],
        ["Tolerance: amount", t.amount],
        ["Tolerance: days (amount + date)", t.days],
        ["Tolerance: days (amount + description)", t.description_days],
        ["Tolerance: minimum similarity", t.min_similarity],
        ["Tolerance: days (group amounts)", t.group_days],
    ]
    for side, inputs in ((A, result.a_inputs), (B, result.b_inputs)):
        for inp in inputs:
            summary.append([f"{side} file {inp.source_file}",
                            f"{inp.summary['clean_rows']} clean rows, {inp.summary['errors']} "
                            f"errors, {inp.summary['warnings']} warnings"])  # fmt: skip
    sheets = [Sheet("Summary", ["Item", "Value"], summary)]
    for status in STATUSES:
        sheets.append(Sheet(labels[status], columns, [row(i) for i in result.of(status)], money))
    sheets.append(Sheet("All items", columns, [row(i) for i in result.items], money))
    issues = [
        [side, *issue.as_output()]
        for side, inputs in ((A, result.a_inputs), (B, result.b_inputs))
        for inp in inputs
        for issue in inp.issues
    ]
    from reconpipe.tools.standardizer import ISSUE_COLUMNS

    sheets.append(Sheet("Input issues", ["Side", *ISSUE_COLUMNS], issues))
    return sheets


def write_report(result: ReconcileResult, output: Path) -> Path:
    from reconpipe.tools.excel_out import write_workbook

    return write_workbook(output, report_sheets(result))
