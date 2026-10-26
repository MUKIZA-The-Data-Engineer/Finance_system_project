-- Tier 1: exact reference. Ledger lines sharing a reference are summed, so one
-- bank transfer that settles several invoices (one-to-many) matches as a whole.
-- Same reference but a different total is not a match: it is an amount_mismatch.
with ledger_by_ref as (
    select
        account_id,
        ref_id,
        sum(amount)                                  as ledger_amount,
        count(*)                                     as ledger_lines,
        list(ledger_entry_id order by ledger_entry_id) as ledger_entry_ids
    from {{ ref('int_ledger_entries') }}
    where not is_duplicate and ref_id is not null
    group by account_id, ref_id
)
select
    b.bank_txn_id,
    l.ledger_entry_ids,
    'tier1_exact'                                                as match_tier,
    case when l.ledger_amount = b.amount then 'matched' else 'amount_mismatch' end as status,
    1.00                                                         as confidence,
    'reference ' || b.ref_id
        || case when l.ledger_lines > 1 then ', ' || l.ledger_lines || ' ledger lines' else '' end
                                                                 as detail
from {{ ref('stg_bank_transactions') }} as b
join ledger_by_ref as l
    on b.account_id = l.account_id and b.ref_id = l.ref_id
