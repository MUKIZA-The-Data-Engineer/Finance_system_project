-- Singular test: every bank account the ledger posts to must have a statement
-- loaded. Otherwise every entry on it would show up as "missing in bank".
select l.account_id, count(*) as ledger_lines
from {{ ref('stg_ledger_entries') }} as l
left join (select distinct account_id from {{ ref('stg_bank_transactions') }}) as b
    using (account_id)
where b.account_id is null
group by l.account_id
