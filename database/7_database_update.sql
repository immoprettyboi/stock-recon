-- =============================================================
-- Stock Recon: update 7 (Takealot shipments / POs and courier cost per shipment)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================

-- One row per Takealot shipment (PO), with what was in it. Filled by the sync.
create table if not exists public.takealot_pos (
  po            text primary key,        -- Takealot PO number, e.g. 191798129
  shipment_id   text,
  reference     text,                    -- e.g. PO-29853221-25/10/2026-JHB3-1
  warehouse     text,                    -- JHB / CPT / DBN
  facility      text,                    -- e.g. JHB3
  due_date      date,
  status        text,                    -- e.g. Shipped, Received
  leadtime      boolean,
  qty_sending   int,
  qty_received  int,
  qty_damaged   int,
  items         jsonb,                   -- [{sku, title, qty}]
  raw           jsonb,
  updated_at    timestamptz not null default now()
);
alter table public.takealot_pos enable row level security;
drop policy if exists "signed in" on public.takealot_pos;
create policy "signed in" on public.takealot_pos for all to authenticated using (true) with check (true);

-- what Courier Guy charged for each shipment (charges minus reversals, incl. VAT)
alter table public.courier_shipments add column if not exists cost numeric(12,2);

do $$ begin
  begin alter publication supabase_realtime add table public.takealot_pos; exception when others then null; end;
end $$;

select 'ok' as done;
