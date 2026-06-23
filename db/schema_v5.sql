-- ============================================================================
-- Podcast Intelligence v5 — multi-tenant schema for Supabase (Postgres)
-- ============================================================================
-- Paste this whole file into the Supabase SQL editor and run it. It is
-- idempotent (safe to re-run). It supersedes db/schema.sql (the single-owner
-- Neon prototype schema), which is kept only for reference during migration.
--
-- Isolation model (see docs/PRD.md §7):
--   * RLS policies below are the *backstop*. They enforce per-user access when
--     the database is reached via the Supabase anon key + a user JWT (PostgREST).
--   * The FastAPI backend connects with the service-role / owner connection
--     string (which BYPASSES RLS), so user-facing routes MUST also filter every
--     query by the JWT-derived user id. The scan/delivery pipeline is the only
--     code that legitimately writes the global tables, via the service role.
-- ============================================================================

-- ── Global catalog (read-all-authenticated, written by the service role only) ──

CREATE TABLE IF NOT EXISTS podcasts (
  id              BIGSERIAL PRIMARY KEY,
  pi_feed_id      TEXT UNIQUE,                 -- Podcast Index feed id
  itunes_id       TEXT,
  rss_url         TEXT UNIQUE NOT NULL,
  title           TEXT,
  publisher       TEXT,
  artwork_url     TEXT,
  description     TEXT,
  categories      TEXT[] DEFAULT '{}',
  is_popular      BOOLEAN NOT NULL DEFAULT FALSE,  -- in the curated scan set
  last_scanned_at TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS episodes (
  id               BIGSERIAL PRIMARY KEY,
  podcast_id       BIGINT NOT NULL REFERENCES podcasts(id) ON DELETE CASCADE,
  pi_episode_id    TEXT,
  guid             TEXT NOT NULL,
  title            TEXT,
  description      TEXT,
  audio_url        TEXT,
  episode_url      TEXT,
  published_at     TIMESTAMPTZ,
  duration_seconds INT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (podcast_id, guid)
);
CREATE INDEX IF NOT EXISTS idx_episodes_podcast   ON episodes(podcast_id);
CREATE INDEX IF NOT EXISTS idx_episodes_published ON episodes(published_at DESC NULLS LAST);

-- One shared summary per episode — the fixed PE lens makes this safe to reuse
-- across every user (docs/PRD.md §7).
CREATE TABLE IF NOT EXISTS episode_summaries (
  id                BIGSERIAL PRIMARY KEY,
  episode_id        BIGINT NOT NULL UNIQUE REFERENCES episodes(id) ON DELETE CASCADE,
  summary_md        TEXT,
  tldr              TEXT,
  target_words      INT,
  transcript_source TEXT,
  model             TEXT,
  style_version     TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS people (
  id              BIGSERIAL PRIMARY KEY,
  name            TEXT NOT NULL,
  normalized_name TEXT UNIQUE,                 -- lower/trimmed for dedup + matching
  bio             TEXT,
  external_ids    JSONB NOT NULL DEFAULT '{}',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS episode_people (
  episode_id BIGINT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  person_id  BIGINT NOT NULL REFERENCES people(id)   ON DELETE CASCADE,
  confidence REAL,
  source     TEXT,                             -- 'pi_tag' | 'llm'
  PRIMARY KEY (episode_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_episode_people_person ON episode_people(person_id);

CREATE TABLE IF NOT EXISTS episode_topics (
  episode_id BIGINT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  topic      TEXT NOT NULL,
  confidence REAL,
  PRIMARY KEY (episode_id, topic)
);
CREATE INDEX IF NOT EXISTS idx_episode_topics_topic ON episode_topics(topic);

-- ── User-scoped tables (RLS: user_id = auth.uid()) ─────────────────────────────

CREATE TABLE IF NOT EXISTS profiles (
  user_id         UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  email           TEXT,
  default_cadence TEXT NOT NULL DEFAULT 'instant'
                  CHECK (default_cadence IN ('instant','daily','weekly')),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS subscriptions (
  id              BIGSERIAL PRIMARY KEY,
  user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  podcast_id      BIGINT NOT NULL REFERENCES podcasts(id) ON DELETE CASCADE,
  cadence_override TEXT CHECK (cadence_override IN ('instant','daily','weekly')),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (user_id, podcast_id)
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id);

CREATE TABLE IF NOT EXISTS tracked_people (
  id         BIGSERIAL PRIMARY KEY,
  user_id    UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  person_id  BIGINT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (user_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_tracked_people_user ON tracked_people(user_id);

CREATE TABLE IF NOT EXISTS tracked_topics (
  id         BIGSERIAL PRIMARY KEY,
  user_id    UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  topic      TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tracked_topics_user_topic
  ON tracked_topics(user_id, lower(topic));

-- One delivery row per (user, episode): the dedup backbone. `reasons` records
-- *every* reason the episode matched (show / person / topic) so a single email
-- can explain all of them. (docs/PRD.md §9)
CREATE TABLE IF NOT EXISTS deliveries (
  id         BIGSERIAL PRIMARY KEY,
  user_id    UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  episode_id BIGINT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  reasons    JSONB NOT NULL DEFAULT '[]',
  status     TEXT NOT NULL DEFAULT 'queued'
             CHECK (status IN ('queued','sent','failed')),
  channel    TEXT NOT NULL DEFAULT 'email',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  sent_at    TIMESTAMPTZ,
  UNIQUE (user_id, episode_id)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_user_created ON deliveries(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status);

-- Deferred feature (engagement-based recs), table created now so it can be
-- populated later without a migration. (docs/PRD.md §14)
CREATE TABLE IF NOT EXISTS engagement (
  id         BIGSERIAL PRIMARY KEY,
  user_id    UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  episode_id BIGINT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  action     TEXT NOT NULL CHECK (action IN ('open','summarize','read')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_engagement_user ON engagement(user_id);

-- ── Ops: per-scan-tick run log (successor to cron_runs) ────────────────────────

CREATE TABLE IF NOT EXISTS scan_runs (
  id                  BIGSERIAL PRIMARY KEY,
  started_at          TIMESTAMPTZ NOT NULL,
  finished_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  shows_scanned       INT NOT NULL DEFAULT 0,
  episodes_ingested   INT NOT NULL DEFAULT 0,
  episodes_matched    INT NOT NULL DEFAULT 0,
  summaries_generated INT NOT NULL DEFAULT 0,
  emails_sent         INT NOT NULL DEFAULT 0,
  errors              JSONB NOT NULL DEFAULT '[]',
  ok                  BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_scan_runs_started ON scan_runs(started_at DESC);

-- ── Auto-provision a profile row when a user signs up ──────────────────────────

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  INSERT INTO public.profiles (user_id, email)
  VALUES (NEW.id, NEW.email)
  ON CONFLICT (user_id) DO NOTHING;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- ── Row-Level Security ─────────────────────────────────────────────────────────
-- Global catalog: any authenticated user may read; writes are service-role only
-- (service role bypasses RLS, so no write policy is granted to normal users).

ALTER TABLE podcasts          ENABLE ROW LEVEL SECURITY;
ALTER TABLE episodes          ENABLE ROW LEVEL SECURITY;
ALTER TABLE episode_summaries ENABLE ROW LEVEL SECURITY;
ALTER TABLE people            ENABLE ROW LEVEL SECURITY;
ALTER TABLE episode_people    ENABLE ROW LEVEL SECURITY;
ALTER TABLE episode_topics    ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS read_podcasts          ON podcasts;
DROP POLICY IF EXISTS read_episodes          ON episodes;
DROP POLICY IF EXISTS read_episode_summaries ON episode_summaries;
DROP POLICY IF EXISTS read_people            ON people;
DROP POLICY IF EXISTS read_episode_people    ON episode_people;
DROP POLICY IF EXISTS read_episode_topics    ON episode_topics;

CREATE POLICY read_podcasts          ON podcasts          FOR SELECT TO authenticated USING (TRUE);
CREATE POLICY read_episodes          ON episodes          FOR SELECT TO authenticated USING (TRUE);
CREATE POLICY read_episode_summaries ON episode_summaries FOR SELECT TO authenticated USING (TRUE);
CREATE POLICY read_people            ON people            FOR SELECT TO authenticated USING (TRUE);
CREATE POLICY read_episode_people    ON episode_people    FOR SELECT TO authenticated USING (TRUE);
CREATE POLICY read_episode_topics    ON episode_topics    FOR SELECT TO authenticated USING (TRUE);

-- User-scoped: a user may only see and modify their own rows.

ALTER TABLE profiles       ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions  ENABLE ROW LEVEL SECURITY;
ALTER TABLE tracked_people ENABLE ROW LEVEL SECURITY;
ALTER TABLE tracked_topics ENABLE ROW LEVEL SECURITY;
ALTER TABLE deliveries     ENABLE ROW LEVEL SECURITY;
ALTER TABLE engagement     ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE
  t   TEXT;
  col TEXT;
BEGIN
  FOR t, col IN
    SELECT * FROM (VALUES
      ('profiles','user_id'),
      ('subscriptions','user_id'),
      ('tracked_people','user_id'),
      ('tracked_topics','user_id'),
      ('deliveries','user_id'),
      ('engagement','user_id')
    ) AS v(t, col)
  LOOP
    EXECUTE format('DROP POLICY IF EXISTS owner_all ON %I', t);
    EXECUTE format(
      'CREATE POLICY owner_all ON %I FOR ALL TO authenticated '
      'USING (auth.uid() = %I) WITH CHECK (auth.uid() = %I)',
      t, col, col
    );
  END LOOP;
END;
$$;

-- ── Migration: titratable summary detail (Quick / Standard / Deep) ─────────────
-- Cache one summary variant per (episode, detail_level) instead of one per episode,
-- and let each user pick their preferred depth (which also drives their deliveries).
-- Idempotent: safe to re-run.

ALTER TABLE episode_summaries
  ADD COLUMN IF NOT EXISTS detail_level TEXT NOT NULL DEFAULT 'standard';
-- Drop the old one-summary-per-episode unique constraint; key on (episode, level).
ALTER TABLE episode_summaries
  DROP CONSTRAINT IF EXISTS episode_summaries_episode_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_episode_summaries_episode_level
  ON episode_summaries (episode_id, detail_level);

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS summary_detail TEXT NOT NULL DEFAULT 'standard'
  CHECK (summary_detail IN ('quick','standard','deep'));

-- ============================================================================
-- Done. Next: the FastAPI backend reads SUPABASE_URL / SUPABASE_ANON_KEY /
-- SUPABASE_SERVICE_ROLE_KEY and a POSTGRES connection string from env.
-- ============================================================================
