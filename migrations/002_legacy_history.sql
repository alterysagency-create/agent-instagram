-- Optional additive import of the original paired public.conversations rows.
-- Run only after 001. Historical events are completed, NEVER sent to Instagram.
begin;
do $$
declare j jsonb; c uuid; e uuid; external text; stamp timestamptz;
begin
 if to_regclass('public.conversations') is null then return; end if;
 for j in execute 'select to_jsonb(t) from public.conversations t order by to_jsonb(t)->>''created_at'',to_jsonb(t)->>''id''' loop
  if j->>'account_id' is null or j->>'sender_id' is null or j->>'user_message' is null then continue; end if;
  if not exists(select 1 from agent_ia.accounts where id=j->>'account_id') then continue; end if;
  external := 'legacy:' || coalesce(j->>'id',md5(j::text));
  stamp := coalesce((j->>'created_at')::timestamptz,now());
  insert into agent_ia.conversations(account_id,sender_id) values(j->>'account_id',j->>'sender_id')
  on conflict(account_id,sender_id) do update set sender_id=excluded.sender_id returning id into c;
  insert into agent_ia.events(conversation_id,external_id,channel,content,status,response,created_at)
  values(c,external,'simulation',left(coalesce(nullif(j->>'user_message',''),'[empty legacy message]'),2000),
  'done',j->>'agent_response',stamp) on conflict(conversation_id,channel,external_id) do nothing;
  select id into e from agent_ia.events where conversation_id=c and channel='simulation' and external_id=external;
  insert into agent_ia.messages(conversation_id,event_id,role,content,created_at)
  values(c,e,'user',j->>'user_message',stamp) on conflict(event_id,role) do nothing;
  if j->>'agent_response' is not null then
   insert into agent_ia.messages(conversation_id,event_id,role,content,created_at)
   values(c,e,'assistant',j->>'agent_response',stamp) on conflict(event_id,role) do nothing;
  end if;
 end loop;
end $$;
commit;
