-- Ledger lines with exact copies flagged. A line identical to an earlier one
-- (same account, date, amount, reference and text) is a suspected double
-- posting: it is reported, and never offered to the matching tiers.
with ranked as (
    select
        *,
        row_number() over copies    as copy_number,
        first_value(ledger_entry_id) over copies as original_entry_id
    from {{ ref('stg_ledger_entries') }}
    window copies as (
        partition by account_id, txn_date, amount, coalesce(ref_id, ''), description_raw
        order by ledger_entry_id
    )
)
select
    * exclude (copy_number, original_entry_id),
    copy_number > 1 as is_duplicate,
    case when copy_number > 1 then original_entry_id end as duplicate_of
from ranked
