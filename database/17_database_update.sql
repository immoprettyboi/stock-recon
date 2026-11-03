-- =============================================================
-- Stock Recon: update 17 (price fighter: minimum profit as a percentage)
-- Paste into Supabase > SQL Editor > New query > Run
-- Safe to run more than once.
-- =============================================================
-- Auto floor = cost + the higher of: min_profit (rand) and min_profit_pct (% of the unit's cost), plus Takealot's fees
alter table public.price_rules add column if not exists min_profit_pct numeric(6,2);
select 'ok' as done;
