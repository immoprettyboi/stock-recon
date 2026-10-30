-- =============================================================
-- Stock Recon: update 11 (set lead-time stock on Takealot from the app)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================

-- A change you ask for in the app; the PC applies it on Takealot and fills in the result
create table if not exists public.leadtime_requests (
  id            bigserial primary key,
  sku           text not null,
  qty           int not null check (qty >= 0),
  requested_by  text,
  requested_at  timestamptz not null default now(),
  done_at       timestamptz,
  ok            boolean,
  result        text
);

alter table public.leadtime_requests enable row level security;
drop policy if exists "signed in" on public.leadtime_requests;
create policy "signed in" on public.leadtime_requests for all to authenticated using (true) with check (true);

do $$ begin
  begin alter publication supabase_realtime add table public.leadtime_requests; exception when others then null; end;
end $$;

select 'ok' as done;
