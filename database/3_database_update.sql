-- =============================================================
-- Stock Recon: update 3 (Takealot stock check)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================

-- What Takealot says it holds for each listing, per warehouse, filled by the sync
alter table public.products add column if not exists takealot_status   text;
alter table public.products add column if not exists takealot_stock    jsonb;
alter table public.products add column if not exists stock_updated_at  timestamptz;

select count(*) as products from public.products;
