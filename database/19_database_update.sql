-- =============================================================
-- Stock Recon: update 19 (supplier invoices)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================

-- the supplier's invoice number each unit was bought on
alter table public.units add column if not exists invoice_no text;
-- which invoice a payment (or an amount owed) is for
alter table public.supplier_ledger add column if not exists invoice_no text;

create index if not exists units_invoice_idx on public.units(supplier, invoice_no);

select 'ok' as done;
