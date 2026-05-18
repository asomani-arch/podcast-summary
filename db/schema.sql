-- Run once against your Vercel Postgres database to create tables.
-- v2 migration: add podcast_index_id, artwork_url, publisher to feeds.
-- v3 migration: add per-feed customization + cron_runs.
-- v4 migration: focus summaries on Overview + Key Takeaways for PE readers.

CREATE TABLE IF NOT EXISTS feeds (
  id SERIAL PRIMARY KEY,
  rss_url TEXT UNIQUE NOT NULL,
  podcast_title TEXT,
  email TEXT NOT NULL,
  podcast_index_id TEXT,          -- Podcast Index feed ID
  artwork_url TEXT,               -- cover art for UI display
  publisher TEXT,                 -- show author / publisher name
  active BOOLEAN DEFAULT TRUE,
  -- v3: per-feed customization
  summary_length TEXT DEFAULT 'standard',   -- 'short' | 'standard' | 'deep'
  sections TEXT[] DEFAULT ARRAY['overview','takeaways'],
  frequency_days INT DEFAULT 1,             -- minimum days between deliveries
  last_delivered_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Migrations for existing databases (safe to run multiple times):
ALTER TABLE feeds ADD COLUMN IF NOT EXISTS podcast_index_id TEXT;
ALTER TABLE feeds ADD COLUMN IF NOT EXISTS artwork_url TEXT;
ALTER TABLE feeds ADD COLUMN IF NOT EXISTS publisher TEXT;
ALTER TABLE feeds ADD COLUMN IF NOT EXISTS summary_length TEXT DEFAULT 'standard';
ALTER TABLE feeds ADD COLUMN IF NOT EXISTS sections TEXT[] DEFAULT ARRAY['overview','takeaways'];
ALTER TABLE feeds ADD COLUMN IF NOT EXISTS frequency_days INT DEFAULT 1;
ALTER TABLE feeds ADD COLUMN IF NOT EXISTS last_delivered_at TIMESTAMPTZ;
ALTER TABLE feeds ALTER COLUMN sections SET DEFAULT ARRAY['overview','takeaways'];

CREATE TABLE IF NOT EXISTS episodes (
  id SERIAL PRIMARY KEY,
  feed_id INT REFERENCES feeds(id) ON DELETE CASCADE,
  guid TEXT NOT NULL,             -- unique episode ID from RSS
  title TEXT,
  published_at TIMESTAMPTZ,
  audio_url TEXT,
  summary TEXT,
  transcript_source TEXT,         -- 'youtube' | 'audio' | 'audio_partial' | 'shownotes'
  emailed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(feed_id, guid)
);

CREATE INDEX IF NOT EXISTS idx_episodes_feed ON episodes(feed_id);
CREATE INDEX IF NOT EXISTS idx_episodes_emailed ON episodes(emailed_at);

-- v3: persisted cron run log so the UI can show last-run status + errors.
CREATE TABLE IF NOT EXISTS cron_runs (
  id SERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL,
  finished_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  feeds_checked INT NOT NULL DEFAULT 0,
  feeds_skipped INT NOT NULL DEFAULT 0,
  new_episodes INT NOT NULL DEFAULT 0,
  errors JSONB NOT NULL DEFAULT '[]'::jsonb,
  ok BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_cron_runs_started ON cron_runs(started_at DESC);
