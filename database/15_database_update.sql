-- =============================================================
-- Stock Recon: update 15 (price fighter: what to do at the floor)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================

-- 'hold'    = stay at the floor while the competitor is below it
-- 'ceiling' = jump back up to the ceiling, wait for the competitor's bot to follow you up, then fight down again
alter table public.price_rules add column if not exists floor_action text not null default 'ceiling';

alter table public.price_log drop constraint if exists price_log_action_check;

select 'ok' as done;
