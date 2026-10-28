---
title: Suggest the Odoo import mapping
description: How this file's columns should map to an Odoo bank-statement or journal import
---
I need to import the transactions in {file_name} into Odoo accounting.
The file has these columns:

{columns}

{sample_rows}

The Odoo import columns available are:

- bank statement lines: {odoo_statement_columns}
- journal entries: {odoo_journal_columns}

1. Say which Odoo import fits this file better, and why, in two sentences.
2. Give a table: file column -> Odoo column (or "not imported").
3. List anything that has to be prepared in Odoo first (journals, partners,
   accounts) and any value that must be transformed (signs, date format).
