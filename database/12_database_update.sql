-- =============================================================
-- Stock Recon: update 12 (debtors: what you owe each supplier)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================

-- Every change to what you owe a supplier:
--   balance  = "I owe them this much as of today" (a starting point; later entries count from here)
--   owe      = an amount you now owe them (e.g. an invoice, or stock not in the app)
--   payment  = money you paid them
-- New stock added in the app with that supplier and a cost is added automatically after the latest balance.
create table if not exists public.supplier_ledger (
  id          bigserial primary key,
  supplier    text not null,
  kind        text not null check (kind in ('balance','owe','payment')),
  date        date not null default current_date,
  amount      numeric(12,2) not null,
  note        text not null default '',
  created_by  text,
  created_at  timestamptz not null default now()
);
create index if not exists supplier_ledger_supplier_idx on public.supplier_ledger(supplier);

alter table public.supplier_ledger enable row level security;
drop policy if exists "signed in" on public.supplier_ledger;
create policy "signed in" on public.supplier_ledger for all to authenticated using (true) with check (true);

do $$ begin
  begin alter publication supabase_realtime add table public.supplier_ledger; exception when others then null; end;
end $$;

select 'ok' as done;
