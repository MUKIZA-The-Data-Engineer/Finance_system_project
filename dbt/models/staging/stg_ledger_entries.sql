-- Ledger side of the reconciliation. Renaming and casting, plus the same text
-- normalisation the bank side got in Python, so descriptions compare like for like.
select
    entry_id                                        as ledger_entry_id,
    account_id,
    date_key                                        as txn_date,
    cast(amount as decimal(18, 2))                  as amount,
    case when amount > 0 then 'in' else 'out' end   as direction,
    nullif(trim(ref_id), '')                        as ref_id,
    description                                     as description_raw,
    trim(regexp_replace(
        regexp_replace(lower(description), '[^a-z0-9\s]', ' ', 'g'),
        '\s+', ' ', 'g'
    ))                                              as description_clean,
    counterparty_key,
    gl_account,
    source_file,
    "row_number"                                    as source_row
from {{ source('warehouse', 'fact_ledger_entries') }}
