-- Hulpfuncties voor RLS naar een niet-publiek schema, zodat ze niet via /rest/v1/rpc aanroepbaar zijn.
create schema if not exists private;
grant usage on schema private to authenticated;

create or replace function private.is_app_user() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from public.app_users
    where email = lower(coalesce(auth.jwt() ->> 'email', ''))
  );
$$;

create or replace function private.is_approver() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from public.app_users
    where email = lower(coalesce(auth.jwt() ->> 'email', '')) and role = 'approver'
  );
$$;

revoke all on function private.is_app_user() from public, anon;
revoke all on function private.is_approver() from public, anon;
grant execute on function private.is_app_user() to authenticated;
grant execute on function private.is_approver() to authenticated;

do $$
declare t text;
begin
  foreach t in array array['app_users','topics','topic_versions','topic_daily','approvals','baskets',
                           'basket_members','portfolio_snapshots','price_bars','api_usage','runs']
  loop
    execute format('drop policy if exists %I on public.%I', case t
      when 'app_users' then 'app_users_read' when 'topics' then 'topics_read'
      when 'topic_versions' then 'topic_versions_read' when 'topic_daily' then 'topic_daily_read'
      when 'approvals' then 'approvals_read' when 'baskets' then 'baskets_read'
      when 'basket_members' then 'basket_members_read' when 'portfolio_snapshots' then 'snapshots_read'
      when 'price_bars' then 'price_bars_read' when 'api_usage' then 'api_usage_read'
      when 'runs' then 'runs_read' end, t);
    execute format('create policy %I on public.%I for select to authenticated using (private.is_app_user())',
                   t || '_read', t);
  end loop;
end $$;

create or replace function public.decide_approval(p_id uuid, p_decision text, p_note text default null)
returns public.approvals
language plpgsql security definer set search_path = public as $$
declare
  r public.approvals;
begin
  if not private.is_approver() then
    raise exception 'Geen rechten om goed te keuren';
  end if;
  if p_decision not in ('approved', 'rejected') then
    raise exception 'Ongeldige beslissing: %', p_decision;
  end if;
  update public.approvals
     set status = p_decision,
         decided_by = lower(auth.jwt() ->> 'email'),
         decided_at = now(),
         note = left(p_note, 500)
   where id = p_id and status = 'pending' and expires_at > now()
  returning * into r;
  if r.id is null then
    raise exception 'Verzoek is niet meer open of verlopen';
  end if;
  return r;
end;
$$;

drop function public.is_app_user();
drop function public.is_approver();
