-- Lock the legacy public tables after their data has been migrated to agent_ia.
-- The backend uses the service-role key and the agent_ia schema.
begin;

do $$
begin
  if to_regclass('public.accounts') is not null then
    alter table public.accounts enable row level security;
    revoke all on table public.accounts from anon, authenticated;
    grant all on table public.accounts to service_role;
  end if;

  if to_regclass('public.conversations') is not null then
    alter table public.conversations enable row level security;
    revoke all on table public.conversations from anon, authenticated;
    grant all on table public.conversations to service_role;
  end if;
end $$;

notify pgrst, 'reload schema';
commit;
