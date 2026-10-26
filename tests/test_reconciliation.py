"""The matching cascade, item by item, against the generator's ground truth."""

from collections import Counter

import truth
from test_dbt import query

BANK = [t for t in truth.EXPECTED["bank_transactions"] if t["expected_status"] != "quarantined"]


def results(db):
    rows = query(
        db,
        "SELECT status, match_tier, bank_source_file, bank_source_row, ledger_entry_ids "
        "FROM recon_results",
    )
    bank = {(f, r): (s, t, sorted(ids)) for s, t, f, r, ids in rows if f is not None}
    ledger_only = {ids[0]: s for s, _, f, _, ids in rows if f is None}
    return bank, ledger_only


def test_every_bank_line_gets_its_expected_status_tier_and_ledger_lines(built_warehouse):
    _, db = built_warehouse
    bank, _ = results(db)
    assert len(bank) == len(BANK)
    wrong = []
    for t in BANK:
        got = bank[(t["file"], t["row_number"])]
        want = (t["expected_status"], t["expected_tier"], sorted(t["ledger_entry_ids"]))
        if t["expected_status"] == "amount_mismatch":
            want = (want[0], "tier1_exact", want[2])  # paired by reference, amounts differ
        if got != want:
            wrong.append((t["file"], t["seq"], t["discrepancy"], got, want))
    assert wrong == []


def test_ledger_only_items_are_classified(built_warehouse):
    _, db = built_warehouse
    _, ledger_only = results(db)
    expected = {e["entry_id"]: e["expected_status"] for e in truth.EXPECTED["ledger_only"]}
    assert ledger_only == expected


def test_match_rate_by_tier_equals_ground_truth(built_warehouse):
    _, db = built_warehouse
    tiers = dict(
        query(
            db, "SELECT match_tier, count(*) FROM recon_results WHERE status = 'matched' GROUP BY 1"
        )
    )
    expected = truth.EXPECTED["expected_matches"]
    assert tiers == {k: expected[k] for k in ("tier1_exact", "tier2_amount_date", "tier3_fuzzy")}


def test_one_to_many_settlements_are_found(built_warehouse):
    _, db = built_warehouse
    rows = query(
        db,
        "SELECT len(ledger_entry_ids) FROM recon_results WHERE status = 'matched' "
        "AND len(ledger_entry_ids) > 1",
    )
    assert len(rows) == truth.EXPECTED["planted"]["one_to_many"]
    assert all(n in (2, 3) for (n,) in rows)


def test_every_status_count(built_warehouse):
    _, db = built_warehouse
    counts = Counter(dict(query(db, "SELECT status, count(*) FROM recon_results GROUP BY 1")))
    expected = Counter(truth.EXPECTED["expected_classification"])
    del expected["quarantined"]  # quarantined lines never reach the warehouse
    assert counts == expected
