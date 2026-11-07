-- =============================================================
-- Stock Recon: update 20 (change history: who changed what, and when)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once. If anything in here ever fails, the change itself still saves.
-- =============================================================

create table if not exists public.audit_log (
  id       bigserial primary key,
  at       timestamptz not null default now(),
  tbl      text not null,
  row_id   text,
  action   text not null,          -- insert / update / delete
  who      text,                   -- the signed-in e-mail, or "server" for the sync
  changes  jsonb                   -- update: {field: [before, after]}; insert/delete: the whole row
);
create index if not exists audit_log_row_idx on public.audit_log(tbl, row_id, at desc);

alter table public.audit_log enable row level security;
drop policy if exists "signed in read" on public.audit_log;
create policy "signed in read" on public.audit_log for select to authenticated using (true);

create or replace function public.stockrecon_audit() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  ch jsonb;
  who text;
  r jsonb;
begin
  begin
    who := coalesce(nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'email', 'server');
    if tg_op = 'UPDATE' then
      select jsonb_object_agg(o.key, jsonb_build_array(o.value, n.value)) into ch
        from jsonb_each(to_jsonb(old)) o join jsonb_each(to_jsonb(new)) n using (key)
       where o.value is distinct from n.value and o.key not in ('updated_at', 'raw');
      if ch is null then return new; end if;
      r := to_jsonb(new);
    elsif tg_op = 'INSERT' then
      r := to_jsonb(new); ch := r - 'raw';
    else
      r := to_jsonb(old); ch := r - 'raw';
    end if;
    insert into public.audit_log(tbl, row_id, action, who, changes)
    values (tg_table_name, r ->> 'id', lower(tg_op), who, ch);
  exception when others then
    null;                            -- never block the real change
  end;
  if tg_op = 'DELETE' then return old; end if;
  return new;
end $$;

drop trigger if exists stockrecon_audit on public.units;
create trigger stockrecon_audit after insert or update or delete on public.units
  for each row execute function public.stockrecon_audit();
drop trigger if exists stockrecon_audit on public.supplier_ledger;
create trigger stockrecon_audit after insert or update or delete on public.supplier_ledger
  for each row execute function public.stockrecon_audit();
drop trigger if exists stockrecon_audit on public.expenses;
create trigger stockrecon_audit after insert or update or delete on public.expenses
  for each row execute function public.stockrecon_audit();

select 'ok' as done;
