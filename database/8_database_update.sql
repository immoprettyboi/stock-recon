-- =============================================================
-- Stock Recon: update 8 (Takealot returns history)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================

-- Every return from Takealot's View Returns page, filled by the sync
create table if not exists public.takealot_returns (
  return_id    text primary key,        -- Takealot's seller_return_id
  rrn          text,                    -- e.g. RRN-D58EZ-9OE4
  order_id     text,
  sku          text,
  title        text,
  qty          int,
  dc           text,
  reason       text,
  outcome      text,                    -- current outcome, e.g. sellable stock / removal order
  return_date  date,
  raw          jsonb,
  updated_at   timestamptz not null default now()
);
create index if not exists takealot_returns_date_idx on public.takealot_returns(return_date);

alter table public.takealot_returns enable row level security;
drop policy if exists "signed in" on public.takealot_returns;
create policy "signed in" on public.takealot_returns for all to authenticated using (true) with check (true);

select 'ok' as done;
