"""Ground truth from tests/expected_results.json, as the statements print it.

A statement prints more than its real transactions (a reprinted line, a
planted 0.00 line) and some lines misprint a value. Tests that compare parsed
rows with the generator use printed() so they all see one consistent view.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = json.loads((ROOT / "tests" / "expected_results.json").read_text(encoding="utf-8"))


def statement(file_name: str) -> dict:
    return next(s for s in EXPECTED["statements"] if Path(s["file"]).name == file_name)


def transactions(file_name: str) -> list[dict]:
    return [t for t in EXPECTED["bank_transactions"] if t["file"] == file_name]


def defects(file_name: str | None = None) -> list[dict]:
    return [d for d in EXPECTED["quality_defects"] if file_name in (None, d["file"])]


def printed(file_name: str) -> list[dict]:
    """Every printed line in order, with the values as printed (misprints included).

    Each line has date, ref_id, description, amount, balance (as strings, date
    ISO), seq (None for a line that is not a real transaction) and defect.
    """
    by_seq = {t["seq"]: t for t in transactions(file_name)}
    extra = iter(d for d in defects(file_name) if d["seq"] is None)
    lines = []
    for seq in statement(file_name)["printed_seqs"]:
        if seq is None:
            d = next(extra)
            lines.append({**d, "ref_id": None, "defect": d["rule"]})
            continue
        t = by_seq[seq]
        lines.append(
            {
                "seq": seq,
                "date": t["printed_date"] or t["date"],
                "ref_id": t["ref_id"],
                "description": t["description"],
                "amount": t["amount"],
                "balance": t["printed_balance"] or t["balance"],
                "defect": t["data_quality"],
            }
        )
    return lines
