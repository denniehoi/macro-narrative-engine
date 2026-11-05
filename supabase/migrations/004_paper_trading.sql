-- Papieren handel: instap- en uitstapkoersen per mandje, rendement, en een papieren portefeuille.
alter table public.baskets
  add column if not exists entry_day date,
  add column if not exists exit_day date,
  add column if not exists perf jsonb not null default '{}'::jsonb;    -- rendement in EUR en lokaal, ACWI ter vergelijking
alter table public.basket_members
  add column if not exists entry_price double precision,
  add column if not exists entry_fx double precision,                  -- EUR per eenheid valuta bij instap
  add column if not exists exit_price double precision,
  add column if not exists exit_fx double precision;
alter table public.portfolio_snapshots
  add column if not exists paper jsonb not null default '{}'::jsonb;   -- nav, cash, posities, ACWI-vergelijking
