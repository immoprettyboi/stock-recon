-- =============================================================
-- Stock Recon: update 18 (payout forecast and other live figures from the server)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================

-- Small named values the server works out and the app shows (e.g. the next Takealot payout).
create table if not exists public.app_state (
  key         text primary key,
  value       jsonb not null default '{}'::jsonb,
  updated_at  timestamptz not null default now()
);

alter table public.app_state enable row level security;
drop policy if exists "signed in" on public.app_state;
create policy "signed in" on public.app_state for all to authenticated using (true) with check (true);

select 'ok' as done;
