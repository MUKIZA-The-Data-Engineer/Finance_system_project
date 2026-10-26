-- A 'matched' item must balance to the cent; anything else is a mismatch.
select *
from {{ ref('recon_results') }}
where status = 'matched' and ledger_amount <> bank_amount
