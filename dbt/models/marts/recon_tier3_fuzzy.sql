-- Tier 3: same account and amount, a wider date window, and evidence in the
-- text. similarity = 0.5 * word overlap + 0.5 * (both sides name the same
-- counterparty). Word overlap is |shared words| / |words in the shorter text|.
with matched_before as (
    select * from {{ ref('recon_tier1_exact') }}
    union all
    select * from {{ ref('recon_tier2_amount_date') }}
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
        abs(date_diff('day', l.txn_date, b.txn_date))                 as days_apart,
        list_distinct(string_split(b.description_clean, ' '))          as bank_words,
        list_distinct(string_split(l.description_clean, ' '))          as ledger_words,
        coalesce(b.counterparty_key = l.counterparty_key, false)       as same_counterparty
    from bank as b
    join ledger as l
        on b.account_id = l.account_id and b.amount = l.amount
    where abs(date_diff('day', l.txn_date, b.txn_date)) <= {{ var('tier3_window_days') }}
),
scored as (
    select
        *,
        len(list_intersect(bank_words, ledger_words))
            / least(len(bank_words), len(ledger_words))               as word_overlap
    from pairs
),
with_similarity as (
    select
        *,
        round(0.5 * word_overlap + 0.5 * same_counterparty::int, 2)   as similarity
    from scored
),
ranked as (
    select
        *,
        row_number() over (partition by bank_txn_id order by similarity desc, days_apart, ledger_entry_id) as bank_rank,
        row_number() over (partition by ledger_entry_id order by similarity desc, days_apart, bank_txn_id) as ledger_rank
    from with_similarity
    where similarity >= {{ var('tier3_min_similarity') }}
)
select
    bank_txn_id,
    [ledger_entry_id]                                         as ledger_entry_ids,
    'tier3_fuzzy'                                             as match_tier,
    'matched'                                                 as status,
    similarity                                                as confidence,
    'same amount, ' || days_apart || ' day(s) apart, similarity ' || similarity as detail
from ranked
where bank_rank = 1 and ledger_rank = 1
