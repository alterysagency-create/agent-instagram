begin;
alter table agent_ia.conversations
  add column if not exists sender_username text,
  add column if not exists sender_name text,
  add column if not exists sender_profile_pic text;
notify pgrst,'reload schema';
commit;
