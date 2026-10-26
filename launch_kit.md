# ReconPipe launch kit (for you, not for the repo)

## CV bullet

Lead with the number; the stack is the footnote.

**ReconPipe: bank reconciliation pipeline** (Python, DuckDB, dbt, Dagster, Streamlit, Docker)
- Built a pipeline that reconciles bank statements in three formats (2 PDF, 1 Excel)
  against the company ledger in about 10 seconds, auto-matching 96.5% of transactions
  and surfacing the rest as an exception report; 100% classification accuracy
  (294/294) against planted ground truth.
- Designed a pluggable parser interface (adding a bank layout = one new file),
  Decimal-safe cleaning with quarantine for invalid rows, an idempotent DuckDB star
  schema, and a three-tier dbt matching cascade with per-match confidence.
- 155 automated tests (95% coverage) and 50 dbt data tests, run in GitHub Actions
  together with the full reconciliation and a Docker build.

Shorter, single-line version:
- Reconciled 288 bank transactions across 3 statement formats in ~10 s with a 96.5%
  auto-match rate and 100% accuracy against ground truth (Python, DuckDB, dbt, Dagster).

## LinkedIn post (one design decision, not "I built a project")

> Adding a second bank to my reconciliation pipeline took one new file.
>
> Bank statements are a mess. One bank sends a PDF with a ruled table. Another sends
> a PDF with no lines at all, one signed amount column, and "balance brought forward"
> rows on every page. A third sends Excel with junk above the header.
>
> The tempting approach is one big parse function with an if-statement per bank.
> It works until the fourth bank, and then every change risks breaking the other three.
>
> Instead, every layout implements the same two-method interface:
> can_parse(file) → "is this mine?" and parse(file) → rows in one shared schema.
> A small registry asks each parser in turn and dispatches the file. If two parsers
> both claim a file, it raises an error instead of guessing, because a wrong guess
> silently misreads every row.
>
> The test of a design is the second implementation. When I wrote the parser for the
> unruled PDF (rebuilt from word positions, since there is no table to extract), I
> touched nothing else: no change to the interface, the registry, or the first parser.
>
> The pipeline now reconciles 288 bank lines against the ledger in about 10 seconds,
> matches 96.5% automatically, and hands the other 16 to a person, each pointing to the
> exact row on both sides.
>
> Code, design decisions and limitations: github.com/MUKIZA-The-Data-Engineer/Finance_system_project
>
> #DataEngineering #Python #dbt #DuckDB #Finance

## GitHub release text

Title: `v1.0.0: bank statement → ledger reconciliation`

Body: paste the `v1.0.0` section of `CHANGELOG.md`.
