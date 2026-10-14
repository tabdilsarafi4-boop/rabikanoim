-- Supabase SQL Editor: run this once.
-- API keys are private server-side settings. Do not add anon/authenticated policies.
create table if not exists public.ai_settings (
  id integer primary key check (id = 1),
  openrouter_keys jsonb not null default '[]'::jsonb,
  pollinations_keys jsonb not null default '[]'::jsonb,
  openrouter_model text not null default 'dots-studio/dots-3-note-preview:free',
  pollinations_model text not null default 'openai/gpt-5.4-nano',
  updated_at timestamptz not null default now()
);

alter table public.ai_settings enable row level security;
-- Intentionally no policies for anon/authenticated. Only the server's service_role key can access it.

create or replace function public.set_ai_settings_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists ai_settings_updated_at on public.ai_settings;
create trigger ai_settings_updated_at
before update on public.ai_settings
for each row execute function public.set_ai_settings_updated_at();

insert into public.ai_settings (id) values (1) on conflict (id) do nothing;
