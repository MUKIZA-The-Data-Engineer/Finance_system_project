# ReconPipe

Bank statement → ledger reconciliation pipeline.

> Work in progress: day 8 of a 16-day build (26 Oct – 10 Nov 2026).
> The full README with the architecture, metrics and design decisions lands on day 15.

## Architecture

Each layer reads only from the layer below it, writes only to the layer above
it, and never edits anything in place.

```
data/raw/          original PDFs and Excel files, immutable
   |  extract/     one parser per bank layout, all emitting RawTransaction
   v
data/staging/      one row per transaction, still dirty, source file tracked
   |  transform/   dates, amounts, text normalisation, de-duplication
   |  validate/    CleanTransaction contract; failures go to data/quarantine/
   v
warehouse.duckdb   fact_transactions, dim_account, dim_counterparty
   |  dbt/         matching cascade: exact ref -> amount+date -> fuzzy description
   v
reports/           matched.csv, exceptions.csv, run_summary.json
```

The contract between layers lives in [`src/reconpipe/schemas.py`](src/reconpipe/schemas.py).

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate        # Windows Git Bash: source .venv/Scripts/activate
pip install -e ".[dev]"
python scripts/generate_statements.py   # writes data/raw/ + tests/expected_results.json
pytest
python -m reconpipe                     # parse, standardise, validate, load warehouse.duckdb
```

Then ask the warehouse a question, for example total debits per account per month:

```sql
SELECT account_id, strftime(date_key, '%Y-%m') AS month,
       sum(-amount) FILTER (WHERE amount < 0) AS total_debits
FROM fact_bank_transactions
GROUP BY ALL ORDER BY ALL;
```

From Python (or any DuckDB client):

```bash
python -c "import duckdb; print(duckdb.connect('warehouse.duckdb').sql('SELECT * FROM pipeline_runs'))"
```

## Warehouse

Star schema in DuckDB ([`src/reconpipe/load/schema.sql`](src/reconpipe/load/schema.sql)).
**Grain: one row per bank statement transaction per source file**, and one row per
ledger journal line. Bank and ledger stay in separate fact tables until matching.

| Table | Holds |
|---|---|
| `fact_bank_transactions` | validated statement rows (quarantined rows never arrive) |
| `fact_ledger_entries` | the company ledger |
| `dim_account`, `dim_counterparty`, `dim_date` | accounts, counterparty names, calendar |
| `pipeline_runs` | one row per run: counts and duration |

Loading is idempotent: each run replaces the rows of the files it loaded, in one
transaction, so re-running never double-loads and a failed load rolls back.

The bank statements are confidential, so ReconPipe ships a seeded generator: three
deliberately different bank layouts (two PDF, one Excel), a ledger with planted
discrepancies, and the ground truth in `tests/expected_results.json`. Your own
real files belong in `data/real/`, which the pipeline never reads.

## Repository layout

| Path | Purpose |
|---|---|
| `src/reconpipe/` | The pipeline package: `extract/`, `transform/`, `validate/`, `load/`, `report/` |
| `scripts/` | Synthetic bank statement and ledger generator |
| `dbt/` | Staging models and the reconciliation matching cascade |
| `orchestration/` | Dagster assets and schedule |
| `app/` | Streamlit review app |
| `tests/` | pytest suite |

The older general-ledger reporting engine (`adapters.py`, `etl_pipeline.py`,
`reports.py`, `run_pipeline.py`, see `system.md`) and the AI-assisted Excel
mapper (`ai_ingestion/`) are separate projects kept at the repository root.
They are not part of ReconPipe.

## Licence

MIT
