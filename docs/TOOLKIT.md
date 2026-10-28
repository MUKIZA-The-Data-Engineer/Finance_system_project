# ReconPipe toolkit: user guide

The pipeline (`python -m reconpipe`) reconciles the three known bank layouts in
`data/raw/`. The toolkit does the same work on **any** bank statement or ledger
file you give it (PDF, Excel `.xlsx`/`.xls`, or CSV), from a web page or from
the command line.

```
messy file ──► read (PDF / Excel / CSV) ──► map columns ──► clean + validate
                                              │ rules, --map, remembered,
                                              │ or Claude (--ai)
             ┌────────────────────────────────┴───────────────────────────┐
             ▼                          ▼                                  ▼
   clean Excel + issues        Odoo-ready import             reconciliation workbook
   (standardize, pdf-to-excel) (odoo-export)                 (reconcile)
```

## The quickest way: upload a file

```bash
source .venv/Scripts/activate          # Windows Git Bash (macOS/Linux: .venv/bin/activate)
streamlit run app/toolkit.py           # opens http://localhost:8501
```

| Tab | Upload | You get |
|---|---|---|
| **Process a file** | one or more statements or ledgers | a clean Excel file, an Odoo import, the list of problems found |
| **Reconcile** | the bank statement(s) and the books | everything above, plus the reconciliation workbook and a summary |
| **AI assistant** | any file | a ready-to-use prompt about it; Claude answers it if you have an API key |

Every result can be downloaded from the page and is also saved under `output/app/`.

## The same from the command line

Two commands do everything in one step:

```bash
reconpipe process statement.pdf                    # one file
reconpipe process inbox                            # every file in a folder
reconpipe workflow --bank statement.pdf --books ledger.xlsx
```

- `process` writes `output/<file name>/`: `<name>_clean.xlsx` (for a PDF,
  `<name>_extracted.xlsx`, which also keeps the raw rows read from the PDF) and
  `<name>_odoo.xlsx`. Drop files into `inbox/` and run `reconpipe process inbox`.
- `workflow` writes `output/workflow_<bank>_vs_<books>/`: the statements
  processed (clean + Odoo), the books cleaned, `reconciliation.xlsx` and
  `summary.txt`. Repeat `--bank`/`--books` for several files, or give a folder.

The five tools underneath, for when you want just one step:

| Command | Does |
|---|---|
| `reconpipe pdf-to-excel FILE.pdf` | PDF bank statement → Excel (clean rows, raw extracted rows, issues) |
| `reconpipe standardize FILE` | any messy PDF/Excel/CSV → clean standard Excel + issues |
| `reconpipe reconcile BANK BOOKS` | two files (or `--a`/`--b` sets) → reconciliation workbook |
| `reconpipe odoo-export FILE` | any file → Odoo import workbook (`--template` for empty ones) |
| `reconpipe ai list / show / run / map-columns` | the prompt library, answered by Claude |

`reconpipe <command> --help` lists every option. If the `reconpipe` command is
not found, use `python -m reconpipe <command>` instead.

### When a column is not recognised

The column names of English and French exports are recognised automatically
("Date opération", "Libellé", "Débit", "Money out", "Txn Ref", ...). When a file
uses names the rules do not know, either say which is which:

```bash
reconpipe standardize odd.csv --map "When=date" --map "How much=amount"
```

or let Claude work it out (`--ai`, or `reconpipe ai map-columns odd.csv`). A mapping
Claude finds is remembered in `config/column_memory.json` and used automatically,
without asking again, for every later file with the same columns.

### Dates like 05/01/2026

The whole file is checked to decide whether dates are day-first or month-first.
When nothing in the file can tell (every day is 12 or less), the rows are
reported as ambiguous instead of guessed: pass `--date-order day-first` (or
`month-first`).

### Reconciliation settings

| Option | Default | Meaning |
|---|---|---|
| `--amount-tolerance` | 0 | amounts this far apart still match |
| `--date-tolerance` | 2 | days apart for amount + date matches |
| `--description-days` | 14 | days apart for amount + description matches |
| `--min-similarity` | 0.4 | text similarity (0-1) for description matches |
| `--group-days` | 7 | days apart for one line settled by several |

Matching runs strictest first: same reference, then same amount and close date,
then same amount and similar description or partner, then one line against a
group of two or three. Each match says which rule made it and how sure it is.

## Odoo settings (`config/odoo.toml`)

Set your real codes before importing; the committed file has `EXAMPLE` values and
every export warns until they are replaced.

```toml
[odoo]
target = "bank_statement_lines"     # or "journal_entries"
journal = "BNK1"                    # your bank journal's short code
bank_account_code = "512100"        # journal entries only
suspense_account_code = "471000"    # journal entries only

[journals]                          # optional: a journal per bank account
"BX-0099-12" = "BNK2"
```

The import workbook's **how to import** sheet gives the Odoo menu path. Lines
with problems (no description, zero amount, failed validation) are left out of
the import and listed on the **issues** sheet.

## The AI assistant

```bash
reconpipe ai list                                   # the prompts
reconpipe ai show pandas_transform statement.xlsx   # the filled-in prompt; nothing is sent
reconpipe ai run odoo_mapping statement.pdf         # Claude's answer, saved in output/
```

| Prompt | Asks Claude for |
|---|---|
| `map_columns` | which column is the date, amount, description... (used by `--ai`) |
| `pandas_transform` | Python (pandas) code that cleans this file's layout |
| `odoo_mapping` | which Odoo import fits, and how the columns map to it |
| `review_issues` | a plain-language explanation of the problems found, with fixes |

Prompts are plain text files in `src/reconpipe/tools/prompts/`; add your own by
copying one and changing it.

**What is sent:** the column names and the first five rows of the file.
`--headers-only` (on by default in the web page) sends the column names only.
`ai show` sends nothing.

**What it needs:** a Claude API key from https://console.anthropic.com, set in the
shell before running:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."      # Git Bash / macOS / Linux
```

Everything else in the toolkit works without a key.

## Requirements on your side

| For | You need |
|---|---|
| everything | `pip install -e ".[dev]"` once after pulling (adds `xlrd` for `.xls` and `anthropic`) |
| the web page | Streamlit, already in `[dev]` (or `pip install -e ".[app]"`) |
| Odoo imports | your journal and account codes in `config/odoo.toml` |
| the AI assistant | `ANTHROPIC_API_KEY` (optional) |
| scanned PDFs | not supported: the PDF must contain text (try selecting text in a PDF viewer) |
