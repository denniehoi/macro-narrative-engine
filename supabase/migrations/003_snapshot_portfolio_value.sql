-- Portefeuillewaarde in de momentopname, zodat het dashboard bedragen in euro kan tonen.
alter table public.portfolio_snapshots add column if not exists portfolio_value_eur double precision;
