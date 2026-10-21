-- Structured facts make a human handoff actionable without rereading everything.
begin;
alter table agent_ia.conversations
  add column if not exists lead_data jsonb not null default '{}'::jsonb,
  add column if not exists handoff_reason text,
  add column if not exists handoff_requested_at timestamptz;
notify pgrst,'reload schema';
commit;
