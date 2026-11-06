-- Betrouwbare ochtendrun: Supabase (pg_cron) roept elke dag om 05:17 UTC de functie trigger-run aan,
-- met een geheim dat alleen de database kent. GitHub's eigen planning blijft als reserve.
create extension if not exists pg_cron;
create extension if not exists pg_net;

create table if not exists private.cron_secret (
  id     int primary key default 1 check (id = 1),
  secret text not null
);
insert into private.cron_secret (secret) values (encode(extensions.gen_random_bytes(24), 'hex'))
on conflict (id) do nothing;
revoke all on private.cron_secret from public, anon, authenticated;

create or replace function public.verify_cron_secret(p_secret text) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (select 1 from private.cron_secret where secret = p_secret);
$$;
revoke all on function public.verify_cron_secret(text) from public, anon, authenticated;
grant execute on function public.verify_cron_secret(text) to service_role;

select cron.schedule('dagelijkse-run', '17 5 * * *', $job$
  select net.http_post(
    url := 'https://cmmikebzpakirfowzqjp.supabase.co/functions/v1/trigger-run',
    headers := jsonb_build_object('Content-Type', 'application/json',
                                  'x-cron-secret', (select secret from private.cron_secret where id = 1)),
    body := '{"source": "planning"}'::jsonb)
$job$);
