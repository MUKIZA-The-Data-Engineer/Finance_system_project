-- Every bank line and every ledger line appears in recon_results exactly once.
with bank as (
    select b.bank_txn_id as item, count(r.result_id) as n
    from {{ ref('stg_bank_transactions') }} as b
    left join {{ ref('recon_results') }} as r using (bank_txn_id)
    group by b.bank_txn_id
),
ledger as (
    select l.ledger_entry_id as item, count(r.result_id) as n
    from {{ ref('stg_ledger_entries') }} as l
    left join (
        select result_id, unnest(ledger_entry_ids) as ledger_entry_id
        from {{ ref('recon_results') }}
    ) as r using (ledger_entry_id)
    group by l.ledger_entry_id
)
select * from bank where n <> 1
union all
select * from ledger where n <> 1
