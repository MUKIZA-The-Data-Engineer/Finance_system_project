---
title: Map columns to the standard fields
description: Which column is the date, the amount, the description... (used by --ai)
---
You are helping a finance team load a bank statement or ledger export into a
reconciliation tool. The file below has these columns:

{columns}

{sample_rows}

Map every column to exactly one of these standard fields, or to "ignore":

{fields}

Rules:
- Each standard field may be used by at most one column.
- A statement needs a date and either one signed amount column or separate
  debit (money out) and credit (money in) columns.
- "balance" is the running balance, never the transaction amount.
- Use "ignore" for anything you are not sure about, and give a confidence
  between 0 and 1 for each column.
