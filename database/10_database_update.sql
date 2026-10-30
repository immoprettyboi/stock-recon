-- =============================================================
-- Stock Recon: update 10 (removal orders: used stock, arrival date, customer comment)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================

-- New or used. Units that come back on a removal order go back into stock as used.
alter table public.units add column if not exists condition text not null default 'new';
-- The removal order a unit came back on, and the day it was delivered to you
alter table public.units add column if not exists removal_order_id text;
alter table public.units add column if not exists arrived_at date;

-- The customer's own comment on a return, from Takealot
alter table public.takealot_returns add column if not exists customer_comment text;

-- link the removal unit that's already in the app
update public.units set removal_order_id = order_id
 where status = 'return_me' and order_id is not null and order_id <> '' and removal_order_id is null
   and comment ilike 'Removal order%';

select 'ok' as done;
