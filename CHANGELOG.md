# Changelog

## v1.0.0 (10 November 2026)

First release of ReconPipe: bank statement → ledger reconciliation, end to end.

**Headline.** Reconciles 288 bank transactions across 3 statement formats in about
10 seconds, auto-matches 96.5% of them, and turns the other 16 into a reviewable
exception report. Every item is classified correctly against ground truth (294/294).

### Ingestion
- Parser interface with a registry that dispatches each file to the one parser
  that recognises it (and refuses to guess when two claim it).
- Three layouts: a ruled PDF table with wrapped descriptions, an unruled PDF with
  a signed amount column and brought/carried-forward lines, and an Excel export
  with junk header rows. Multi-page statements and page-break reprints handled.
- A separate reader for the ledger export.

### Cleaning and validation
- One ISO date per file, with ambiguous files rejected rather than guessed.
- `Decimal` money only, with debit and credit collapsed into one signed amount
  and the original columns kept.
- Clean descriptions next to the raw ones.
- De-duplication keyed on account, date, amount, text and running balance.
- Pydantic contracts plus business rules (no future dates, no zero amounts,
  balance continuity). Failures are quarantined with their rule and source row,
  never dropped.

### Warehouse and matching
- DuckDB star schema with an idempotent per-file loader and a `pipeline_runs`
  table.
- dbt staging models and a three-tier matching cascade (exact reference with
  one-to-many, amount + date, fuzzy text). Every match carries its tier,
  confidence and detail.
- 50 dbt models and data tests, including custom generic and singular tests.

### Reporting, orchestration and serving
- `exceptions.csv`, `matched.csv` and `run_summary.json`, with match rate by tier,
  value reconciled and in exception, and an accuracy check against ground truth.
- Six Dagster assets with four asset checks and a daily schedule.
- A Streamlit review app: headline metrics, a filterable and downloadable
  exceptions table, match rate by tier, and a data-quality panel.

### Engineering
- A seeded synthetic data generator with per-line ground truth and
  byte-identical output.
- 155 pytest tests (95% line coverage), including an end-to-end test from raw
  files to final match rate.
- Docker image and `docker compose up`; GitHub Actions for lint, tests, the full
  reconciliation, `dbt build` and the Docker image.
