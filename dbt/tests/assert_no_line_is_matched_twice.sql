-- A ledger line may settle at most one bank line, across all tiers.
select ledger_entry_id, count(*) as times_used
from (
    select unnest(ledger_entry_ids) as ledger_entry_id
    from {{ ref('recon_results') }}
    where status in ('matched', 'amount_mismatch')
)
group by ledger_entry_id
having count(*) > 1
