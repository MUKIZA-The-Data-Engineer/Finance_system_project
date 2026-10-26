-- Tier 2: same account, same amount, dates within a small window. Only what
-- tier 1 left unmatched takes part, and a pairing is kept only when it is the
-- best for both sides (closest date), so no line is ever matched twice.
with matched_before as (
    select * from {{ ref('recon_tier1_exact') }}
),
bank as (
    select * from {{ ref('stg_bank_transactions') }}
    where bank_txn_id not in (select bank_txn_id from matched_before)
),
ledger as (
    select * from {{ ref('int_ledger_entries') }}
    where not is_duplicate
      and ledger_entry_id not in (select unnest(ledger_entry_ids) from matched_before)
),
pairs as (
    select
        b.bank_txn_id,
        l.ledger_entry_id,
        abs(date_diff('day', l.txn_date, b.txn_date)) as days_apart
    from bank as b
    join ledger as l
        on b.account_id = l.account_id and b.amount = l.amount
    where abs(date_diff('day', l.txn_date, b.txn_date)) <= {{ var('tier2_window_days') }}
),
ranked as (
    select
        *,
        row_number() over (partition by bank_txn_id order by days_apart, ledger_entry_id) as bank_rank,
        row_number() over (partition by ledger_entry_id order by days_apart, bank_txn_id) as ledger_rank
    from pairs
)
select
    bank_txn_id,
    [ledger_entry_id]                               as ledger_entry_ids,
    'tier2_amount_date'                             as match_tier,
    'matched'                                       as status,
    round(1.0 - 0.05 * days_apart, 2)               as confidence,
    'same amount, ' || days_apart || ' day(s) apart' as detail
from ranked
where bank_rank = 1 and ledger_rank = 1
