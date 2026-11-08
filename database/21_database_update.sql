-- =============================================================
-- Stock Recon: update 21 (stock adjustments: units removed, with a full record kept)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================

create table if not exists public.stock_adjustments (
  id          bigserial primary key,
  at          timestamptz not null default now(),
  who         text,
  kind        text not null default 'remove',      -- remove / restore
  reason      text not null,                       -- e.g. Added by mistake, Duplicate, Lost, Damaged, Written off
  note        text,
  product     text,
  qty         int not null default 0,
  value       numeric(12,2) not null default 0,    -- total cost of the units
  off_debtors boolean not null default false,      -- also taken off what you owe the supplier
  units       jsonb not null default '[]'::jsonb,  -- the removed units, exactly as they were
  restored_at timestamptz
);
create index if not exists stock_adjustments_at_idx on public.stock_adjustments(at desc);

alter table public.stock_adjustments enable row level security;
drop policy if exists "signed in" on public.stock_adjustments;
create policy "signed in" on public.stock_adjustments for all to authenticated using (true) with check (true);

select 'ok' as done;
