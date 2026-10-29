-- =============================================================
-- Stock Recon: update 9 (Takealot's monthly Stock Recon report)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================

-- One row per month: Takealot's Stock Recon file, downloaded by the sync on the 1st
create table if not exists public.takealot_recons (
  month          text primary key,          -- e.g. 2026-10
  task_id        text,
  file_name      text,
  csv            text,                      -- the file itself, so the app can download it
  rows           jsonb,                     -- every product line
  missing        jsonb,                     -- lines where Takealot lost / wrote off stock
  missing_units  int not null default 0,
  claim_value    numeric(12,2) not null default 0,
  claim_status   text not null default 'open',   -- open / claimed / paid / nothing
  claim_ref      text,
  claim_date     date,
  downloaded_at  timestamptz not null default now()
);

alter table public.takealot_recons enable row level security;
drop policy if exists "signed in" on public.takealot_recons;
create policy "signed in" on public.takealot_recons for all to authenticated using (true) with check (true);

select 'ok' as done;
