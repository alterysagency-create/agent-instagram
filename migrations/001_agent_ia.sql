-- Additive migration: existing public tables are never altered or deleted.
begin;
create schema if not exists agent_ia;
create table if not exists agent_ia.accounts (
 id text primary key default gen_random_uuid()::text,
 business_name text not null, email text, instagram_handle text,
 instagram_id text unique, instagram_token_env text,
 system_prompt text not null, plan text not null default 'starter',
 created_at timestamptz not null default now()
);
create table if not exists agent_ia.conversations (
 id uuid primary key default gen_random_uuid(),
 account_id text not null references agent_ia.accounts(id),
 sender_id text not null, human_required boolean not null default false,
 created_at timestamptz not null default now(), unique(account_id,sender_id)
);
create table if not exists agent_ia.events (
 id uuid primary key default gen_random_uuid(),
 conversation_id uuid not null references agent_ia.conversations(id),
 external_id text not null, channel text not null check(channel in ('instagram','simulation')),
 content text not null check(length(content) between 1 and 2000),
 status text not null default 'pending' check(status in
 ('pending','processing','retry','sending','done','handoff','expired','failed','delivery_unknown')),
 response text, attempts int not null default 0, error_code text,
 created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
 available_at timestamptz not null default now(), unique(conversation_id,channel,external_id)
);
create table if not exists agent_ia.messages (
 id bigint generated always as identity primary key,
 conversation_id uuid not null references agent_ia.conversations(id),
 event_id uuid references agent_ia.events(id),
 role text not null check(role in ('user','assistant')), content text not null,
 created_at timestamptz not null default now(), unique(event_id,role)
);
create index if not exists message_context on agent_ia.messages(conversation_id,created_at,id);
create index if not exists event_queue on agent_ia.events(status,available_at,created_at);

create or replace function agent_ia.enqueue(p_account text,p_sender text,p_content text,p_external text,p_channel text,p_occurred timestamptz default null)
returns setof agent_ia.events language plpgsql set search_path = agent_ia,pg_temp as $$
declare c uuid;
begin
 insert into conversations(account_id,sender_id) values(p_account,p_sender)
 on conflict(account_id,sender_id) do update set sender_id=excluded.sender_id returning id into c;
 insert into events(conversation_id,content,external_id,channel,created_at) values(c,p_content,p_external,p_channel,coalesce(p_occurred,now()))
 on conflict(conversation_id,channel,external_id) do nothing;
 return query select * from events where conversation_id=c and channel=p_channel and external_id=p_external;
end $$;

create or replace function agent_ia.claim() returns setof agent_ia.events
language plpgsql set search_path = agent_ia,pg_temp as $$
declare e events;
begin
 -- Serialize claim decisions across workers. A lease exceeds all upstream timeouts.
 perform pg_advisory_xact_lock(84179021);
 update events set status='delivery_unknown',error_code='WorkerLostDuringSend',updated_at=now()
 where status='sending' and updated_at < now()-interval '10 minutes';
 update conversations set human_required=true where id in
 (select conversation_id from events where status='delivery_unknown');
 update events set status=case when attempts>=5 then 'failed' else 'retry' end,updated_at=now()
 where status='processing' and updated_at < now()-interval '10 minutes';
 select q.* into e from events q where q.status in ('pending','retry') and q.available_at<=now()
 and not exists (select 1 from events earlier where earlier.conversation_id=q.conversation_id
 and earlier.id<>q.id and (earlier.status in ('processing','sending') or
 (earlier.status in ('pending','retry') and (earlier.created_at,earlier.id)<(q.created_at,q.id))))
 order by q.created_at,q.id limit 1 for update skip locked;
 if e.id is null then return; end if;
 update events set status='processing',attempts=attempts+1,updated_at=now() where id=e.id returning * into e;
 insert into messages(conversation_id,event_id,role,content) values(e.conversation_id,e.id,'user',e.content)
 on conflict(event_id,role) do nothing;
 return next e;
end $$;

create or replace function agent_ia.save_response(p_event uuid,p_response text) returns void
language plpgsql set search_path = agent_ia,pg_temp as $$
begin
 update events set response=p_response,updated_at=now() where id=p_event;
 -- Response enters memory only after delivery (or simulation completion).
end $$;
create or replace function agent_ia.finish(p_event uuid,p_status text,p_error text default null) returns void
language plpgsql set search_path = agent_ia,pg_temp as $$
declare e events;
begin
 select * into strict e from events where id=p_event for update;
 update events set status=case when p_status='retry' and attempts>=5 then 'failed' else p_status end,
 error_code=p_error,updated_at=now(),available_at=now()+ make_interval(secs=>least(300,5*power(2,attempts)::int))
 where id=p_event;
 if p_status='done' and e.response is not null then
 insert into messages(conversation_id,event_id,role,content) values(e.conversation_id,e.id,'assistant',e.response)
 on conflict(event_id,role) do nothing;
 end if;
 if p_status='retry' and e.attempts>=5 then
 update conversations set human_required=true where id=e.conversation_id;
 end if;
end $$;

-- Preserve existing businesses, including their original IDs.
do $$ begin
 if to_regclass('public.accounts') is not null then
 execute 'insert into agent_ia.accounts(id,business_name,email,instagram_handle,system_prompt,plan)
 select j->>''id'',coalesce(j->>''business_name'',''Business''),j->>''email'',j->>''instagram_handle'',
 coalesce(j->>''system_prompt'',''Réponds uniquement avec les informations fournies.''),coalesce(j->>''plan'',''starter'')
 from (select to_jsonb(a) j from public.accounts a) s where j->>''id'' is not null on conflict(id) do nothing';
 end if;
end $$;
-- Legacy paired conversations can be imported using 002 after inspecting their schema.
alter table agent_ia.accounts enable row level security;
alter table agent_ia.conversations enable row level security;
alter table agent_ia.messages enable row level security;
alter table agent_ia.events enable row level security;
revoke all on schema agent_ia from public,anon,authenticated;
revoke all on all tables in schema agent_ia from public,anon,authenticated;
revoke all on all functions in schema agent_ia from public,anon,authenticated;
grant usage on schema agent_ia to service_role;
grant all on all tables in schema agent_ia to service_role;
grant usage,select on all sequences in schema agent_ia to service_role;
grant execute on all functions in schema agent_ia to service_role;
notify pgrst,'reload schema';
commit;
