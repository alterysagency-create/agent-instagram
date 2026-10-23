-- Google Calendar appointments and generated pre-call briefs.
-- Additive only: existing conversations and messages are preserved.
begin;

alter table agent_ia.accounts
  add column if not exists booking_url text,
  add column if not exists calendar_id text;

create table if not exists agent_ia.appointments (
  id uuid primary key default gen_random_uuid(),
  account_id text not null references agent_ia.accounts(id),
  conversation_id uuid references agent_ia.conversations(id),
  provider text not null default 'google' check (provider in ('google')),
  external_id text not null,
  status text not null default 'pending' check (status in
    ('pending','processing','retry','ready','unmatched','cancelled','failed')),
  title text not null,
  start_at timestamptz not null,
  end_at timestamptz not null,
  timezone text,
  attendee_name text,
  attendee_email text,
  instagram_username text,
  match_method text,
  meeting_url text,
  event_url text,
  event_payload jsonb not null default '{}'::jsonb,
  source_updated_at timestamptz,
  brief jsonb,
  brief_text text,
  attempts integer not null default 0,
  error_code text,
  available_at timestamptz not null default now(),
  generated_at timestamptz,
  notion_page_id text,
  notion_synced_at timestamptz,
  notion_sync_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(account_id, provider, external_id),
  check(end_at > start_at)
);

create index if not exists appointment_queue
  on agent_ia.appointments(status,available_at,start_at);
create index if not exists appointment_conversation
  on agent_ia.appointments(conversation_id,start_at);

create or replace function agent_ia.claim_appointment()
returns setof agent_ia.appointments
language plpgsql set search_path = agent_ia,pg_temp as $$
declare a appointments;
begin
  perform pg_advisory_xact_lock(84179022);
  update appointments
     set status=case when attempts>=5 then 'failed' else 'retry' end,
         error_code='WorkerLeaseExpired', updated_at=now(),
         available_at=now()+interval '1 minute'
   where status='processing' and updated_at < now()-interval '10 minutes';

  select q.* into a
    from appointments q
   where q.status in ('pending','retry') and q.available_at<=now()
   order by q.start_at,q.created_at
   limit 1 for update skip locked;
  if a.id is null then return; end if;

  update appointments
     set status='processing',attempts=attempts+1,error_code=null,updated_at=now()
   where id=a.id returning * into a;
  return next a;
end $$;

create or replace function agent_ia.finish_appointment(
  p_id uuid, p_status text, p_error text default null,
  p_brief jsonb default null, p_text text default null)
returns void language plpgsql set search_path = agent_ia,pg_temp as $$
begin
  if p_status not in ('ready','unmatched','cancelled','retry','failed') then
    raise exception 'invalid appointment status';
  end if;
  update appointments
     set status=case when p_status='retry' and attempts>=5 then 'failed' else p_status end,
         error_code=p_error,
         brief=coalesce(p_brief,brief),
         brief_text=coalesce(p_text,brief_text),
         generated_at=case when p_status='ready' then now() else generated_at end,
         available_at=case when p_status='retry'
           then now()+make_interval(secs=>least(300,5*power(2,attempts)::int))
           else available_at end,
         updated_at=now()
   where id=p_id;
end $$;

alter table agent_ia.appointments enable row level security;
revoke all on agent_ia.appointments from public,anon,authenticated;
grant all on agent_ia.appointments to service_role;
grant execute on function agent_ia.claim_appointment() to service_role;
grant execute on function agent_ia.finish_appointment(uuid,text,text,jsonb,text) to service_role;
notify pgrst,'reload schema';
commit;
