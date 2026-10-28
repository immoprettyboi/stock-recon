-- =============================================================
-- Stock Recon: update 6 (expenses: Courier Guy top-ups)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================

-- Money going out that isn't the cost of a unit. Courier Guy top-ups are filled by the sync.
create table if not exists public.expenses (
  id           text primary key,          -- e.g. courier-40832557
  date         date not null,
  category     text not null,             -- courier_topup, ...
  amount       numeric(12,2) not null,    -- positive = money spent
  description  text not null default '',
  source       text not null default 'app',
  raw          jsonb,
  created_at   timestamptz not null default now()
);
create index if not exists expenses_date_idx on public.expenses(date);

alter table public.expenses enable row level security;
drop policy if exists "signed in" on public.expenses;
create policy "signed in" on public.expenses for all to authenticated using (true) with check (true);

do $$ begin
  begin alter publication supabase_realtime add table public.expenses; exception when others then null; end;
end $$;

select 'ok' as done;
