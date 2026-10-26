# ReconPipe

Bank statement → ledger reconciliation pipeline.

> Work in progress: day 3 of a 16-day build (26 Oct – 10 Nov 2026).
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
```

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
