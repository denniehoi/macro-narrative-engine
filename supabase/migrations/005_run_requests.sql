-- Handmatige runs vanuit het dashboard (via de edge function trigger-run), met wie en wanneer.
create table public.run_requests (
  id            uuid primary key default gen_random_uuid(),
  requested_by  text not null,
  requested_at  timestamptz not null default now(),
  status        text not null check (status in ('sent', 'failed', 'refused')),
  message       text
);
create index run_requests_at_idx on public.run_requests (requested_at desc);
alter table public.run_requests enable row level security;
create policy run_requests_read on public.run_requests for select to authenticated using (private.is_app_user());
