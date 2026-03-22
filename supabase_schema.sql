-- ══════════════════════════════════════════════════════════════════════
-- PackGen — Complete Supabase Schema v2
-- Paste this entire file into the Supabase SQL Editor and run it.
-- Safe to re-run: uses IF NOT EXISTS / OR REPLACE throughout.
-- ══════════════════════════════════════════════════════════════════════

-- ── User profiles ──────────────────────────────────────────────────────
create table if not exists packgen_profiles (
  id                    uuid default gen_random_uuid() primary key,
  user_id               uuid references auth.users(id) on delete cascade unique not null,
  plan                  text default 'free' not null check (plan in ('free','pro','admin')),
  usage_this_month      integer default 0 not null,
  packgen_key_hash      text unique,
  packgen_key_prefix    text,
  key_created_at        timestamptz,
  key_last_used_at      timestamptz,
  anthropic_api_key     text default '' not null,
  created_at            timestamptz default now(),
  updated_at            timestamptz default now()
);

create index if not exists idx_packgen_profiles_user_id  on packgen_profiles(user_id);
create index if not exists idx_packgen_profiles_key_hash on packgen_profiles(packgen_key_hash)
  where packgen_key_hash is not null;

-- ── Per-question scores ────────────────────────────────────────────────
create table if not exists packgen_scores (
  id          bigserial primary key,
  user_id     uuid references auth.users(id) on delete cascade not null,
  paper_key   text        not null,   -- e.g. "june_2022"
  q_num       smallint    not null,   -- question number
  score       smallint    not null,   -- marks obtained
  max_marks   smallint    not null,   -- marks available
  topic       text,
  subtopic    text,
  difficulty  smallint,
  pack_num    smallint,               -- which pack this came from
  created_at  timestamptz default now()
);

create index if not exists idx_scores_user    on packgen_scores(user_id);
create index if not exists idx_scores_topic   on packgen_scores(user_id, topic);
create index if not exists idx_scores_paper   on packgen_scores(user_id, paper_key);
create index if not exists idx_scores_ts      on packgen_scores(user_id, created_at desc);

-- ── Pack attempt records ───────────────────────────────────────────────
-- One row per pack the user completes.
create table if not exists packgen_attempts (
  id           bigserial primary key,
  user_id      uuid references auth.users(id) on delete cascade not null,
  jid          text        not null,   -- job id (links to session)
  pack_num     smallint    not null,
  total_score  smallint    not null,
  total_marks  smallint    not null,
  pct          smallint    not null,   -- 0-100
  q_count      smallint    not null,
  created_at   timestamptz default now()
);

create index if not exists idx_attempts_user on packgen_attempts(user_id);
create index if not exists idx_attempts_ts   on packgen_attempts(user_id, created_at desc);

-- ── updated_at trigger ─────────────────────────────────────────────────
create or replace function update_updated_at()
returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end;
$$;

drop trigger if exists packgen_profiles_updated_at on packgen_profiles;
create trigger packgen_profiles_updated_at
  before update on packgen_profiles
  for each row execute function update_updated_at();

-- ── Row Level Security ─────────────────────────────────────────────────
alter table packgen_profiles enable row level security;
alter table packgen_scores    enable row level security;
alter table packgen_attempts  enable row level security;

-- Profiles
drop policy if exists "profiles_select" on packgen_profiles;
drop policy if exists "profiles_update" on packgen_profiles;
create policy "profiles_select" on packgen_profiles for select using (auth.uid() = user_id);
create policy "profiles_update" on packgen_profiles for update using (auth.uid() = user_id);

-- Scores
drop policy if exists "scores_select" on packgen_scores;
drop policy if exists "scores_insert" on packgen_scores;
create policy "scores_select" on packgen_scores for select using (auth.uid() = user_id);
create policy "scores_insert" on packgen_scores for insert with check (auth.uid() = user_id);

-- Attempts
drop policy if exists "attempts_select" on packgen_attempts;
drop policy if exists "attempts_insert" on packgen_attempts;
create policy "attempts_select" on packgen_attempts for select using (auth.uid() = user_id);
create policy "attempts_insert" on packgen_attempts for insert with check (auth.uid() = user_id);

-- ── Auto-create profile on signup ─────────────────────────────────────
create or replace function handle_new_user()
returns trigger language plpgsql security definer as $$
begin
  insert into public.packgen_profiles (user_id, plan, usage_this_month)
  values (new.id, 'free', 0)
  on conflict (user_id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function handle_new_user();

-- ── Monthly usage reset (requires pg_cron) ────────────────────────────
-- Enable pg_cron: Database → Extensions → pg_cron, then uncomment:
--
-- select cron.schedule(
--   'reset-monthly-usage',
--   '0 0 1 * *',
--   $$update public.packgen_profiles set usage_this_month = 0$$
-- );

-- ── Verify ─────────────────────────────────────────────────────────────
-- select table_name from information_schema.tables
-- where table_schema = 'public' and table_name like 'packgen_%';
