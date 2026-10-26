-- One row per reconciliation item: every bank line, plus every ledger line no
-- tier consumed. Each row says how it was classified and points back to the
-- source file and row on both sides.
with matched as (
    select * from {{ ref('recon_tier1_exact') }}
    union all
    select * from {{ ref('recon_tier2_amount_date') }}
    union all
    select * from {{ ref('recon_tier3_fuzzy') }}
),
ledger as (
    select * from {{ ref('int_ledger_entries') }}
),
matched_ledger as (
    select
        m.bank_txn_id,
        sum(l.amount)                                            as ledger_amount,
        min(l.txn_date)                                          as ledger_date,
        string_agg(l.description_raw, ' | ' order by l.ledger_entry_id) as ledger_description,
        string_agg(l.source_row::varchar, ',' order by l.ledger_entry_id) as ledger_source_rows
    from matched as m, unnest(m.ledger_entry_ids) as u(ledger_entry_id)
    join ledger as l on l.ledger_entry_id = u.ledger_entry_id
    group by m.bank_txn_id
),
consumed as (
    select unnest(ledger_entry_ids) as ledger_entry_id from matched
),
bank_items as (
    select
        b.bank_txn_id                                   as item_key,
        coalesce(m.status, 'missing_in_ledger')         as status,
        m.match_tier,
        m.confidence,
        m.detail,
        b.account_id,
        b.bank_txn_id,
        b.txn_date                                      as bank_date,
        b.amount                                        as bank_amount,
        b.ref_id                                        as bank_ref,
        b.description_raw                               as bank_description,
        b.source_file                                   as bank_source_file,
        b.source_row                                    as bank_source_row,
        coalesce(m.ledger_entry_ids, [])                as ledger_entry_ids,
        ml.ledger_date,
        ml.ledger_amount,
        ml.ledger_description,
        ml.ledger_source_rows
    from {{ ref('stg_bank_transactions') }} as b
    left join matched as m using (bank_txn_id)
    left join matched_ledger as ml using (bank_txn_id)
),
ledger_items as (
    select
        l.ledger_entry_id                               as item_key,
        case when l.is_duplicate then 'duplicate_suspected' else 'missing_in_bank' end as status,
        null                                            as match_tier,
        null                                            as confidence,
        case when l.is_duplicate then 'identical to ' || l.duplicate_of
             else 'no bank line matches' end            as detail,
        l.account_id,
        null                                            as bank_txn_id,
        null                                            as bank_date,
        null                                            as bank_amount,
        null                                            as bank_ref,
        null                                            as bank_description,
        null                                            as bank_source_file,
        null                                            as bank_source_row,
        [l.ledger_entry_id]                             as ledger_entry_ids,
        l.txn_date                                      as ledger_date,
        l.amount                                        as ledger_amount,
        l.description_raw                               as ledger_description,
        l.source_row::varchar                           as ledger_source_rows
    from ledger as l
    where l.ledger_entry_id not in (select ledger_entry_id from consumed)
)
select
    md5(item_key)                                       as result_id,
    *,
    ledger_amount - bank_amount                         as difference
from (
    select * from bank_items
    union all by name
    select * from ledger_items
)
