-- ReconPipe warehouse: a small star schema in DuckDB.
--
-- Grain
--   fact_bank_transactions  one row per bank statement transaction per source file
--   fact_ledger_entries     one row per ledger journal line
-- Bank and ledger stay in separate facts until the matching layer (dbt);
-- mixing them earlier would destroy the comparison.
--
-- Every statement is idempotent (IF NOT EXISTS), so this file runs on every load.

CREATE TABLE IF NOT EXISTS dim_date (
    date_key     DATE PRIMARY KEY,
    year         INTEGER NOT NULL,
    quarter      INTEGER NOT NULL,
    month        INTEGER NOT NULL,
    month_name   VARCHAR NOT NULL,
    day          INTEGER NOT NULL,
    weekday      VARCHAR NOT NULL,
    is_weekend   BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_account (
    account_id      VARCHAR PRIMARY KEY,
    statement_file  VARCHAR,          -- bank statement this account appears in
    in_ledger       BOOLEAN NOT NULL  -- whether the books have entries on it
);

CREATE TABLE IF NOT EXISTS dim_counterparty (
    counterparty_key  VARCHAR PRIMARY KEY,  -- casefolded name
    name              VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_bank_transactions (
    txn_hash           VARCHAR PRIMARY KEY,
    account_id         VARCHAR NOT NULL,
    date_key           DATE NOT NULL,
    amount             DECIMAL(18, 2) NOT NULL,  -- signed: money in positive
    debit              DECIMAL(18, 2),
    credit             DECIMAL(18, 2),
    balance            DECIMAL(18, 2),
    ref_id             VARCHAR,
    description_raw    VARCHAR NOT NULL,
    description_clean  VARCHAR NOT NULL,
    counterparty_key   VARCHAR,                  -- NULL when no known name is in the text
    source_file        VARCHAR NOT NULL,
    row_number         INTEGER NOT NULL,
    run_id             VARCHAR NOT NULL,
    UNIQUE (source_file, row_number)
);

CREATE TABLE IF NOT EXISTS fact_ledger_entries (
    entry_id          VARCHAR PRIMARY KEY,
    account_id        VARCHAR NOT NULL,
    date_key          DATE NOT NULL,
    amount            DECIMAL(18, 2) NOT NULL,   -- signed, bank account's view
    ref_id            VARCHAR,
    description       VARCHAR NOT NULL,
    counterparty_key  VARCHAR,
    gl_account        VARCHAR,
    source_file       VARCHAR NOT NULL,
    row_number        INTEGER NOT NULL,
    run_id            VARCHAR NOT NULL
);

-- Observability: one row per run, appended, never replaced.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id              VARCHAR PRIMARY KEY,
    started_at          TIMESTAMP NOT NULL,
    finished_at         TIMESTAMP NOT NULL,
    duration_seconds    DOUBLE NOT NULL,
    files_parsed        INTEGER NOT NULL,
    files_skipped       INTEGER NOT NULL,
    rows_in             INTEGER NOT NULL,
    duplicates_removed  INTEGER NOT NULL,
    rows_quarantined    INTEGER NOT NULL,
    bank_rows_loaded    INTEGER NOT NULL,
    ledger_rows_loaded  INTEGER NOT NULL
);

-- Rows the validate layer quarantined in the latest run (replaced every load),
-- so the review app can show data quality next to the reconciliation.
CREATE TABLE IF NOT EXISTS quarantined_rows (
    run_id        VARCHAR NOT NULL,
    rules         VARCHAR NOT NULL,   -- ';'-separated rule names
    reasons       VARCHAR NOT NULL,
    source_file   VARCHAR NOT NULL,
    row_number    INTEGER NOT NULL,
    account_id    VARCHAR NOT NULL,
    date          VARCHAR,            -- as read: a quarantined date may be invalid
    description   VARCHAR,
    amount        VARCHAR,
    balance       VARCHAR
);
