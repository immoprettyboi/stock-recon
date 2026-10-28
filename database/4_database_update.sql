-- =============================================================
-- Stock Recon: update 4 (status "Missing: query with Takealot")
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================

alter table public.units drop constraint if exists units_status_check;
alter table public.units add constraint units_status_check
  check (status in ('in_stock','awaiting_fees','settled','return_takealot','return_me','missing_query'));

-- when the query was sent, and Takealot's reference/ticket number
alter table public.units add column if not exists query_date date;
alter table public.units add column if not exists query_ref  text not null default '';

select 'ok' as done;
