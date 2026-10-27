-- =============================================================
-- Stock Recon: update 2 (for the Takealot Chrome sync)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================

alter table public.takealot_sales add column if not exists match_note   text;
alter table public.takealot_sales add column if not exists user_ignored boolean not null default false;
create index if not exists takealot_sales_status_idx on public.takealot_sales(match_status);

alter table public.sync_runs add column if not exists requested boolean not null default false;

-- Let the app ask for a "Sync now" (the PC program picks it up within a minute)
drop policy if exists "signed in request" on public.sync_runs;
create policy "signed in request" on public.sync_runs for insert to authenticated with check (requested = true);

-- Takealot SKUs -> your product names (learned from your October sales)
insert into public.products (sku, tsin, takealot_title, product) values
 ('9902369296722','102726222','Playstation 5 Slim Digital Parallel Import','Playstation 5 Slim Digital'),
 ('9902313805499','96098810','PlayStation 5 Slim Digital Edition Console (PS5 Slim)','Playstation 5 Slim Digital'),
 ('9902313805239','96098813','PlayStation 5 Slim Console with disc drive (PS5 Slim)','Playstation 5 Slim Disk'),
 ('9902548423604','102449067','Playstation 5 Slim Disk - Fortnite Voucher included','Playstation 5 Slim Disk - Fortnite Voucher included'),
 ('9902331515875','74104561','Playstation 5 Dualsense Controller - Glacier White (PS5)','Playstation 5 Dualsense Controller - Glacier White (PS5)'),
 ('9902673331270','102773461','Playstation 5 Pro 2TB','PS5 PRO'),
 ('9902354687788','103097025','Samsung Galaxy S26 Ultra 12/256GB-Parallel Import 36-Month Warranty - Black','Samsung Galaxy S26 Ultra 12/256GB-Parallel Import 36-Month Warranty - Black'),
 ('9902327267887','74150964','Xbox Series X 1TB Disc Edition','Xbox Series X 1TB Disc Edition'),
 ('9902313805741','101716974','PlayStation 5 Digital Edition 825GB - Fortnite Flowering Chaos Bundle','PlayStation 5 Digital Edition 825GB - Fortnite Flowering Chaos Bundle'),
 ('9902403413269','69555899','3-in-1 Hair Dryer, Volumiser & Styler with Ceramic Heater','__ignore__')
on conflict (sku) do update set tsin = excluded.tsin, takealot_title = excluded.takealot_title,
  product = coalesce(public.products.product, excluded.product);

select count(*) as products_linked from public.products where product is not null;
