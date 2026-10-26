-- Bank side of the reconciliation. Renaming and casting only: no business logic.
select
    txn_hash                                        as bank_txn_id,
    account_id,
    date_key                                        as txn_date,
    cast(amount as decimal(18, 2))                  as amount,
    case when amount > 0 then 'in' else 'out' end   as direction,
    nullif(trim(ref_id), '')                        as ref_id,
    description_raw,
    description_clean,
    counterparty_key,
    source_file,
    "row_number"                                    as source_row
from {{ source('warehouse', 'fact_bank_transactions') }}
