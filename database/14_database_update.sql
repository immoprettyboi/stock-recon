-- =============================================================
-- Stock Recon: update 14 (phone notifications)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================

-- One row per phone / computer that tapped "Turn on notifications" in the app
create table if not exists public.push_subscriptions (
  endpoint     text primary key,
  keys         jsonb not null,
  user_email   text,
  device       text,
  topics       text[] not null default array['price'],
  created_at   timestamptz not null default now(),
  last_sent_at timestamptz
);

alter table public.push_subscriptions enable row level security;
drop policy if exists "signed in" on public.push_subscriptions;
create policy "signed in" on public.push_subscriptions for all to authenticated using (true) with check (true);

select 'ok' as done;
