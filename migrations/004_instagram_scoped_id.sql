-- Instagram Login exposes both a stable Instagram user_id and an app-scoped id.
-- Keep both so incoming webhooks can be routed reliably and outgoing messages
-- can use the identifier returned for the current app token.
begin;
alter table agent_ia.accounts
  add column if not exists instagram_scoped_id text unique;
notify pgrst, 'reload schema';
commit;
