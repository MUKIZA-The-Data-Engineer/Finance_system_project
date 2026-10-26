# Design decisions

The trade-offs behind ReconPipe, and what each one cost. The code is the record of
*what* was built; this is the record of *why*.

---

## 1. DuckDB, not Postgres

**Decision.** The warehouse is a single DuckDB file.

**Why.** The data is small (hundreds to low thousands of lines a month per
company) and the workload is analytical: group-bys, window functions, set
comparisons. DuckDB is columnar, runs in-process with no server to install or
operate, and speaks the same SQL dbt targets. A reviewer runs the whole project
with `pip install` or `docker compose up`, nothing else.

**Cost.** One writer at a time, and no concurrent multi-user access. The review
app therefore opens the file read-only, and a load and the app should not run at
the very same moment. If the data or the number of users grew, the dbt models
would move to Postgres or a cloud warehouse with a profile change, not a rewrite.

## 2. No Spark, no Kafka

**Decision.** Plain Python for parsing and validation, DuckDB for storage, dbt
for matching.

**Why.** A month of statements is kilobytes. A distributed engine would add a
cluster, serialization, and minutes of start-up to a job that takes about ten
seconds, and would make the project harder to run and to read. Choosing the
smallest tool that fits is the engineering judgement on display here.

**Cost.** None at this scale. The layer boundaries (extract, transform, validate,
load, match) are where a bigger engine would slot in if volumes ever demanded it.

## 3. One parser per bank layout, behind one interface (adapter pattern)

**Decision.** Every layout implements `BaseStatementParser` with `can_parse()` and
`parse()`, and all parsers emit the same `RawTransaction`. A registry asks each
parser whether it recognises a file; exactly one must say yes.

**Why.** Bank layouts change and multiply; the rest of the pipeline must not care.
The proof came on day 5: the second PDF layout (unruled columns, one signed amount
column, brought/carried-forward lines) needed **one new file and one line** in the
registry. `base.py` and the first parser did not change.

**Why refuse ambiguity.** If two parsers claim a file, the registry raises instead
of picking one. A wrong guess misreads every row silently; a loud error costs a
minute.

**Cost.** A new bank still needs a parser. That is deliberate: layouts are where
the real-world mess lives, and each one deserves tests against its own fixture.

## 4. Parsers keep values as printed text

**Decision.** `RawTransaction` holds `"15 Mar 2026"` and `"RWF -1,250.00"` as
strings; the transform layer interprets them.

**Why.** Parsing a layout and interpreting a value are different jobs with
different failure modes. Keeping them apart means a date-format bug is fixed in
one place for every bank, and the raw row in the quarantine file shows exactly
what was on the page.

## 5. Money is `Decimal`, never float

**Decision.** Amounts are `Decimal` with two places, and the schema rejects
floats outright.

**Why.** `0.1 + 0.2 != 0.3` in binary floating point, and a reconciliation is a
test of equality to the cent. Floats from Excel cells go through
`Decimal(str(value))`.

## 6. Ambiguous dates fail loudly

**Decision.** Each file's date format is inferred from all its rows. If two
formats read the file equally well (every day is 12 or under, so `05/01/2026`
could be 5 January or 1 May), the file is rejected rather than guessed.

**Why.** A silently swapped day and month moves transactions between months and
breaks every date-window match after it. One bad date, by contrast, only fails
its own row: the format that reads the most rows wins.

## 7. De-duplication includes the running balance

**Decision.** The duplicate key is a hash of account, date, amount, cleaned
description **and the printed balance**.

**Why.** Two genuine identical purchases on one day (two coffees, same café, same
price) have the same date, amount and text, but different running balances. A
line reprinted at a page break has the same balance. Leaving the balance out
would delete a real transaction.

## 8. Quarantine, never drop

**Decision.** Rows that fail a rule are written to `data/quarantine/` (and the
`quarantined_rows` table) with the rule, the reason, and the source file and row.

**Why.** A dropped row is an invisible error in a financial report. A quarantined
row is a to-do with an address. The run summary counts them by rule, and a Dagster
check fails the run if the quarantine rate jumps.

**Detail: balance continuity.** A naive check flags two lines for one mistake
(the bad line, and the next line checked against the bad balance). Each line is
instead allowed to follow from either the previous line's printed balance or the
balance it should have had, so a misprinted balance and a misread amount each
flag exactly one line.

## 9. Bank and ledger stay apart until matching

**Decision.** Two fact tables (`fact_bank_transactions`, `fact_ledger_entries`),
joined only in the dbt matching models.

**Why.** Reconciliation is a comparison. Merging the two sides earlier would bake
assumptions into the data that the matching layer is supposed to test.

## 10. Matching is a cascade in SQL, and every match says how it was made

**Decision.** Three dbt models, each seeing only what the previous ones could not
match: exact reference (summing ledger lines that share it, for one-to-many
settlements), then amount and date within two days, then amount, a wider window
and text similarity. Every match carries `match_tier`, `confidence` and a `detail`
string. A pairing is kept only if it is the best candidate for both sides, so no
line is ever used twice.

**Why.** The strictest evidence goes first, so a weak rule never steals a line a
strong rule would have matched. Writing the cascade in SQL makes each tier a
model with its own tests and lineage, readable by an analyst as well as an
engineer. "Same reference, different total" is reported as an `amount_mismatch`
rather than a match or a miss, because that is exactly what an accountant needs
to look at.

**Tier 3 similarity.** Word overlap alone was weak on real phrasing ("POS 2743
SIMBA SUPERMARKET KIGALI RW" against "Payment - Simba Supermarket - office
supplies" share only two words). The score is half word overlap and half "both
sides name the same counterparty", with the threshold chosen after measuring the
candidate pairs, not guessed.

## 11. Loads are idempotent by source file

**Decision.** Each load deletes and re-inserts the rows of the files it carries,
inside one transaction, and appends one row to `pipeline_runs`.

**Why.** Re-running must never double-load, a regenerated file must not leave
stale rows, and a failure half-way must leave yesterday's data intact. All three
are tested.

## 12. Synthetic data with ground truth

**Decision.** A seeded generator writes three bank layouts and a ledger with
planted problems, and records the expected outcome of every line in
`tests/expected_results.json`.

**Why.** Real statements are confidential, and "it looks right" is not a test.
With ground truth, accuracy is a number: every one of the 294 reconciliation items
is checked, and so is every planted statement defect. Output is byte-identical
for a given seed, so regenerating never creates noise in git.

## 13. Orchestration wraps the pipeline, it does not reimplement it

**Decision.** The six Dagster assets call the same functions as
`python -m reconpipe`.

**Why.** One implementation, two ways to run it. Dagster adds the DAG, retries,
metadata, a schedule and asset checks; it is not a second copy of the logic that
could drift.
