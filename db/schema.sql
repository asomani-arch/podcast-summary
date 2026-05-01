-- Run once against your Vercel Postgres database to create tables.
-- v2 migration: add podcast_index_id, artwork_url, publisher to feeds.

CREATE TABLE IF NOT EXISTS feeds (
  id SERIAL PRIMARY KEY,
  rss_url TEXT UNIQUE NOT NULL,
  podcast_title TEXT,
  email TEXT NOT NULL,
  podcast_index_id TEXT,          -- Podcast Index feed ID
  artwork_url TEXT,               -- cover art for UI display
  publisher TEXT,                 -- show author / publisher name
  active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Migration for existing databases (safe to run multiple times):
ALTER TABLE feeds ADD COLUMN IF NOT EXISTS podcast_index_id TEXT;
ALTER TABLE feeds ADD COLUMN IF NOT EXISTS artwork_url TEXT;
ALTER TABLE feeds ADD COLUMN IF NOT EXISTS publisher TEXT;

CREATE TABLE IF NOT EXISTS episodes (
  id SERIAL PRIMARY KEY,
  feed_id INT REFERENCES feeds(id) ON DELETE CASCADE,
  guid TEXT NOT NULL,             -- unique episode ID from RSS
  title TEXT,
  published_at TIMESTAMPTZ,
  audio_url TEXT,
  summary TEXT,
  transcript_source TEXT,         -- 'youtube' | 'shownotes' | 'audio'
  emailed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(feed_id, guid)
);

CREATE INDEX IF NOT EXISTS idx_episodes_feed ON episodes(feed_id);
CREATE INDEX IF NOT EXISTS idx_episodes_emailed ON episodes(emailed_at);
