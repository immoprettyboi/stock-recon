-- =============================================================
-- Stock Recon: update 16 (barcode scanner)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================

-- Which product each box barcode belongs to. The app learns these the first time you scan a new box.
create table if not exists public.barcodes (
  code        text primary key,
  product     text not null,
  created_by  text,
  created_at  timestamptz not null default now()
);

alter table public.barcodes enable row level security;
drop policy if exists "signed in" on public.barcodes;
create policy "signed in" on public.barcodes for all to authenticated using (true) with check (true);

select 'ok' as done;
