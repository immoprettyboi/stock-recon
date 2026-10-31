-- =============================================================
-- Stock Recon: update 13 (Price fighter: win the Buy Box)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================

-- Your rules per Takealot listing, set in the app. The PC checks the Buy Box and changes your price.
create table if not exists public.price_rules (
  sku              text primary key,
  enabled          boolean not null default false,
  mode             text not null default 'watch' check (mode in ('watch','fight')),   -- watch = only show what it would do
  floor_mode       text not null default 'auto' check (floor_mode in ('auto','manual')),
  min_profit       numeric(12,2) not null default 500,     -- auto floor: cost + Takealot fees + this
  floor_price      numeric(12,2),                          -- manual floor
  ceiling_price    numeric(12,2),                          -- never above this
  step             numeric(12,2) not null default 1,       -- undercut the Buy Box by this much
  check_minutes    int not null default 10,                -- how often to check this listing
  raise_when_winning boolean not null default true,        -- creep back up when you hold the Buy Box
  raise_every_minutes int not null default 120,
  max_changes_day  int not null default 20,
  -- filled in by the PC
  last_checked     timestamptz,
  my_price         numeric(12,2),
  bb_price         numeric(12,2),
  bb_seller        text,
  winning          boolean,
  floor_used       numeric(12,2),
  last_action      text,
  last_change_at   timestamptz,
  updated_at       timestamptz not null default now()
);

-- Every price change (and what it would have done in watch mode)
create table if not exists public.price_log (
  id          bigserial primary key,
  sku         text not null,
  at          timestamptz not null default now(),
  my_price    numeric(12,2),
  bb_price    numeric(12,2),
  bb_seller   text,
  new_price   numeric(12,2),
  action      text not null,       -- lowered / raised / would_lower / would_raise / at_floor / failed
  note        text
);
create index if not exists price_log_sku_idx on public.price_log(sku, at desc);

alter table public.price_rules enable row level security;
drop policy if exists "signed in" on public.price_rules;
create policy "signed in" on public.price_rules for all to authenticated using (true) with check (true);
alter table public.price_log enable row level security;
drop policy if exists "signed in" on public.price_log;
create policy "signed in" on public.price_log for all to authenticated using (true) with check (true);

do $$ begin
  begin alter publication supabase_realtime add table public.price_rules; exception when others then null; end;
end $$;

select 'ok' as done;
