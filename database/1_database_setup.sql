
-- =============================================================
-- Stock Recon: database setup
-- Paste all of this into Supabase > SQL Editor > New query > Run
-- =============================================================

create extension if not exists pgcrypto;

-- One row per physical unit you buy
create table if not exists public.units (
  id            uuid primary key default gen_random_uuid(),
  product       text not null,
  sku           text,
  sn            text not null default '',
  stock_type    text not null default 'ICASA',
  supplier      text not null default '',
  location      text not null default 'HOME',
  received      date,
  po            text not null default '',
  order_id      text not null default '',
  sold_date     date,
  gross         numeric(12,2),
  fees          numeric(12,2),
  cost          numeric(12,2) not null default 0,
  status        text not null default 'in_stock'
                check (status in ('in_stock','awaiting_fees','settled','return_takealot','return_me')),
  return_date   date,
  comment       text not null default '',
  flag          boolean not null default false,
  match_method  text,            -- import | order | po | fifo | manual
  source        text not null default 'app',
  sheet_row     int,
  created_by    uuid default auth.uid(),
  created_at    timestamptz not null default now(),
  updated_by    uuid,
  updated_at    timestamptz not null default now()
);
create index if not exists units_status_idx  on public.units(status);
create index if not exists units_order_idx   on public.units(order_id);
create index if not exists units_product_idx on public.units(product);

-- Links a Takealot SKU to your own product name (set once per product)
create table if not exists public.products (
  sku            text primary key,
  tsin           text,
  takealot_title text,
  product        text,           -- your name for it, as used in units.product
  updated_at     timestamptz not null default now()
);

-- Every sale line the Chrome sync reads from the Seller Portal
create table if not exists public.takealot_sales (
  order_id       text not null,
  sku            text not null default '',
  order_date     timestamptz,
  sale_status    text,
  product_title  text,
  tsin           text,
  qty            int,
  fulfilment_dc  text,
  customer_dc    text,
  gross          numeric(12,2),
  fees           numeric(12,2),
  net            numeric(12,2),
  po             text,
  raw            jsonb,
  unit_id        uuid references public.units(id) on delete set null,
  match_status   text not null default 'unmatched'
                 check (match_status in ('matched','unmatched','ignored')),
  first_seen     timestamptz not null default now(),
  last_seen      timestamptz not null default now(),
  primary key (order_id, sku)
);

-- One row each time the sync runs, so the app can show "last synced"
create table if not exists public.sync_runs (
  id          bigserial primary key,
  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  ok          boolean,
  message     text,
  rows_read   int,
  matched     int,
  unmatched   int
);

-- keep updated_at fresh
create or replace function public.touch_updated() returns trigger language plpgsql as $$
begin new.updated_at = now(); if auth.uid() is not null then new.updated_by = auth.uid(); end if; return new; end $$;
drop trigger if exists units_touch on public.units;
create trigger units_touch before update on public.units for each row execute function public.touch_updated();

-- Only signed-in people (you and your partner) can see or change anything
alter table public.units          enable row level security;
alter table public.products       enable row level security;
alter table public.takealot_sales enable row level security;
alter table public.sync_runs      enable row level security;

drop policy if exists "signed in" on public.units;
create policy "signed in" on public.units          for all to authenticated using (true) with check (true);
drop policy if exists "signed in" on public.products;
create policy "signed in" on public.products       for all to authenticated using (true) with check (true);
drop policy if exists "signed in" on public.takealot_sales;
create policy "signed in" on public.takealot_sales for all to authenticated using (true) with check (true);
drop policy if exists "signed in read" on public.sync_runs;
create policy "signed in read" on public.sync_runs for select to authenticated using (true);

-- live updates in the app
do $$ begin
  begin alter publication supabase_realtime add table public.units; exception when others then null; end;
  begin alter publication supabase_realtime add table public.sync_runs; exception when others then null; end;
end $$;

