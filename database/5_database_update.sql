-- =============================================================
-- Stock Recon: update 5 (Courier Guy shipments)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================

-- One row per Courier Guy shipment to or from a Takealot warehouse, filled by the sync
create table if not exists public.courier_shipments (
  id                     text primary key,      -- the portal's shipment id
  waybill                text,                   -- e.g. WC9QVV
  reference              text,                   -- customer reference, e.g. "PO 191784976"
  status                 text,
  direction              text,                   -- to_takealot | from_takealot | other
  takealot_dc            text,                   -- JHB / CPT / DBN
  collection_name        text,
  delivery_name          text,
  service                text,
  parcels                int,
  created_at             timestamptz,
  expected_collection    date,
  expected_delivery_from date,
  expected_delivery_to   date,
  delivered_at           timestamptz,
  note                   text,                   -- e.g. reason for a changed collection date
  raw                    jsonb,
  updated_at             timestamptz not null default now()
);

alter table public.courier_shipments enable row level security;
drop policy if exists "signed in" on public.courier_shipments;
create policy "signed in" on public.courier_shipments for all to authenticated using (true) with check (true);

-- a unit can point at the courier shipment it travels on
alter table public.units add column if not exists courier_shipment_id text;

do $$ begin
  begin alter publication supabase_realtime add table public.courier_shipments; exception when others then null; end;
end $$;

select 'ok' as done;
