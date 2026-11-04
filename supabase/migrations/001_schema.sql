-- Macro Narrative Engine: schema, toegangsregels en goedkeurings-RPC.
-- De pipeline schrijft met de secret/service-role key (omzeilt RLS).
-- Het dashboard leest met de publishable/anon key + login; alleen e-mails in app_users zien data.

create extension if not exists pgcrypto;

-- Wie mag het dashboard zien (allowlist) ---------------------------------------
create table public.app_users (
  email        text primary key check (email = lower(email)),
  display_name text,
  role         text not null default 'approver' check (role in ('viewer', 'approver')),
  created_at   timestamptz not null default now()
);

create or replace function public.is_app_user() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from public.app_users
    where email = lower(coalesce(auth.jwt() ->> 'email', ''))
  );
$$;

create or replace function public.is_approver() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from public.app_users
    where email = lower(coalesce(auth.jwt() ->> 'email', '')) and role = 'approver'
  );
$$;

-- Topics -------------------------------------------------------------------------
create table public.topics (
  id                uuid primary key default gen_random_uuid(),
  slug              text not null unique,
  title             text not null,
  status            text not null check (status in ('radar', 'on_deck', 'active', 'retired', 'merged')),
  regime            text not null default 'structural' check (regime in ('structural', 'shock')),
  version           int  not null default 1,
  main_macro_driver text not null,
  keywords          text[] not null default '{}',
  benchmarks        jsonb not null default '[]'::jsonb,   -- [{symbol, direction}] vast per versie
  horizon_class     text check (horizon_class in ('lt_3m', '3_6m', '6_12m', 'gt_12m')),
  polymarket_slugs  text[] not null default '{}',
  source_event_uris text[] not null default '{}',
  mmp               int,
  high_impact       boolean not null default false,
  r_score           double precision,
  streak_in         int not null default 0,
  gate_fail_streak  int not null default 0,
  low_i_streak      int not null default 0,
  protected_until   date,
  first_seen        date not null default current_date,
  promoted_at       date,
  demoted_at        date,
  retired_at        date,
  merged_into       uuid references public.topics(id),
  last_driver_change date,
  meta              jsonb not null default '{}'::jsonb,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create index topics_status_idx on public.topics (status);

-- Versiebeheer van driver/keywords/benchmarks (punt 5: geen stille identiteitsdrift)
create table public.topic_versions (
  topic_id          uuid not null references public.topics(id) on delete cascade,
  version           int  not null,
  title             text,
  main_macro_driver text not null,
  keywords          text[] not null default '{}',
  benchmarks        jsonb not null default '[]'::jsonb,
  horizon_class     text,
  reason            text,
  created_at        timestamptz not null default now(),
  primary key (topic_id, version)
);

-- Dagelijkse meting per topic ------------------------------------------------------
create table public.topic_daily (
  topic_id         uuid not null references public.topics(id) on delete cascade,
  day              date not null,
  status           text,
  articles         int,
  volume_z         double precision,
  sentiment        double precision,
  alignment        double precision,
  backward_looking double precision,
  r_score          double precision,
  n                double precision,
  m                double precision,
  d                double precision,
  p                double precision,
  i_score          double precision,
  gates            jsonb not null default '{}'::jsonb,
  gates_pass       boolean,
  stale            boolean not null default false,
  notes            jsonb not null default '{}'::jsonb,
  primary key (topic_id, day)
);
create index topic_daily_day_idx on public.topic_daily (day);

-- Handmatige goedkeuringen (shocks en merges) --------------------------------------
create table public.approvals (
  id             uuid primary key default gen_random_uuid(),
  kind           text not null check (kind in ('shock', 'merge')),
  topic_id       uuid not null references public.topics(id) on delete cascade,
  other_topic_id uuid references public.topics(id) on delete cascade,
  summary        text not null,
  payload        jsonb not null default '{}'::jsonb,
  status         text not null default 'pending'
                 check (status in ('pending', 'approved', 'rejected', 'expired', 'applied')),
  created_at     timestamptz not null default now(),
  expires_at     timestamptz not null,
  decided_by     text,
  decided_at     timestamptz,
  note           text
);
create index approvals_status_idx on public.approvals (status);

-- Mandjes per Active Topic -----------------------------------------------------------
create table public.baskets (
  id            uuid primary key default gen_random_uuid(),
  topic_id      uuid not null references public.topics(id) on delete cascade,
  topic_version int not null,
  reason        text not null,
  status        text not null default 'active' check (status in ('active', 'exit', 'archived')),
  cash_share    double precision not null default 0,
  notes         jsonb not null default '{}'::jsonb,
  created_at    timestamptz not null default now()
);
create index baskets_topic_idx on public.baskets (topic_id, status);

create table public.basket_members (
  basket_id    uuid not null references public.baskets(id) on delete cascade,
  symbol       text not null,
  name         text,
  role         text,
  tier         text not null check (tier in ('pure_play', 'pick_and_shovel', 'second_order')),
  exposure_pct double precision,
  pre_revenue  boolean not null default false,
  rationale    text,
  source_url   text,
  sigma        double precision,
  addv_usd     double precision,
  currency     text,
  cluster      int,
  weight       double precision not null,
  primary key (basket_id, symbol)
);

-- Dagelijkse portefeuille-momentopname ----------------------------------------------
create table public.portfolio_snapshots (
  day                date primary key,
  topic_weights      jsonb not null default '[]'::jsonb,  -- pie chart van vandaag
  cash               double precision not null,
  targets            jsonb not null default '[]'::jsonb,  -- doelgewichten sinds laatste rebalance
  target_cash        double precision,
  is_rebalance       boolean not null default false,
  rebalance_reason   text,
  last_rebalance_day date,
  drift_max          double precision,
  currency_exposure  jsonb not null default '{}'::jsonb,
  heat_index         double precision,
  created_at         timestamptz not null default now()
);

-- Koerscache, verbruik en runs ---------------------------------------------------------
create table public.price_bars (
  symbol text not null,
  day    date not null,
  close  double precision,
  volume double precision,
  primary key (symbol, day)
);

create table public.api_usage (
  day     date not null,
  service text not null,
  units   double precision not null default 0,
  primary key (day, service)
);

create table public.runs (
  id                uuid primary key default gen_random_uuid(),
  run_day           date not null,
  started_at        timestamptz not null default now(),
  finished_at       timestamptz,
  status            text not null default 'running' check (status in ('running', 'ok', 'partial', 'failed')),
  dry_run           boolean not null default false,
  summary           jsonb not null default '{}'::jsonb,
  errors            jsonb not null default '[]'::jsonb,
  newsapi_tokens    double precision not null default 0,
  llm_input_tokens  int not null default 0,
  llm_output_tokens int not null default 0,
  web_searches      int not null default 0
);

-- Row level security: alleen allowlist leest; niemand schrijft via de API behalve de pipeline.
alter table public.app_users           enable row level security;
alter table public.topics              enable row level security;
alter table public.topic_versions      enable row level security;
alter table public.topic_daily         enable row level security;
alter table public.approvals           enable row level security;
alter table public.baskets             enable row level security;
alter table public.basket_members      enable row level security;
alter table public.portfolio_snapshots enable row level security;
alter table public.price_bars          enable row level security;
alter table public.api_usage           enable row level security;
alter table public.runs                enable row level security;

create policy app_users_read      on public.app_users           for select to authenticated using (public.is_app_user());
create policy topics_read         on public.topics              for select to authenticated using (public.is_app_user());
create policy topic_versions_read on public.topic_versions      for select to authenticated using (public.is_app_user());
create policy topic_daily_read    on public.topic_daily         for select to authenticated using (public.is_app_user());
create policy approvals_read      on public.approvals           for select to authenticated using (public.is_app_user());
create policy baskets_read        on public.baskets             for select to authenticated using (public.is_app_user());
create policy basket_members_read on public.basket_members      for select to authenticated using (public.is_app_user());
create policy snapshots_read      on public.portfolio_snapshots for select to authenticated using (public.is_app_user());
create policy price_bars_read     on public.price_bars          for select to authenticated using (public.is_app_user());
create policy api_usage_read      on public.api_usage           for select to authenticated using (public.is_app_user());
create policy runs_read           on public.runs                for select to authenticated using (public.is_app_user());

-- Goedkeuren/afwijzen kan alleen via deze functie, alleen door approvers, alleen zolang open.
create or replace function public.decide_approval(p_id uuid, p_decision text, p_note text default null)
returns public.approvals
language plpgsql security definer set search_path = public as $$
declare
  r public.approvals;
begin
  if not public.is_approver() then
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

revoke all on function public.decide_approval(uuid, text, text) from public, anon;
grant execute on function public.decide_approval(uuid, text, text) to authenticated;
revoke all on function public.is_app_user() from public, anon;
revoke all on function public.is_approver() from public, anon;
grant execute on function public.is_app_user() to authenticated;
grant execute on function public.is_approver() to authenticated;
