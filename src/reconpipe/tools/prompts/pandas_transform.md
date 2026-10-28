---
title: Python (pandas) code to clean this file
description: A pandas script that turns this file's columns into the standard layout
---
Here are the columns of a bank statement from {file_name}:

{columns}

{sample_rows}

Write Python code using pandas that:
1. reads the file ({file_kind}) into a DataFrame,
2. renames the columns to: {fields},
3. parses dates and amounts (amounts may use spaces or commas as thousand
   separators and a comma as the decimal mark),
4. builds one signed amount column (money in positive, money out negative)
   when the file has separate debit and credit columns,
5. drops empty rows, opening/closing balance lines and totals,
6. writes the result to an Excel file.

Explain each step in one short comment. Keep the code under 60 lines.
