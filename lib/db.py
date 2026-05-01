"""Postgres helpers. Uses POSTGRES_URL env var auto-injected by Vercel/Neon."""
import os
from datetime import datetime

import psycopg
from psycopg.rows import dict_row


def get_conn():
    url = os.getenv("POSTGRES_URL")
    if not url:
        raise RuntimeError("POSTGRES_URL not set")
    return psycopg.connect(url, row_factory=dict_row)


def add_feed(
    rss_url: str,
    podcast_title: str,
    email: str,
    podcast_index_id: str = "",
    artwork_url: str = "",
    publisher: str = "",
) -> int:
    """Insert or reactivate a feed subscription."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO feeds (rss_url, podcast_title, email, podcast_index_id, artwork_url, publisher)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (rss_url) DO UPDATE
              SET podcast_title     = EXCLUDED.podcast_title,
                  podcast_index_id  = COALESCE(NULLIF(EXCLUDED.podcast_index_id, ''), feeds.podcast_index_id),
                  artwork_url       = COALESCE(NULLIF(EXCLUDED.artwork_url, ''),      feeds.artwork_url),
                  publisher         = COALESCE(NULLIF(EXCLUDED.publisher, ''),         feeds.publisher),
                  active            = TRUE
            RETURNING id
            """,
            (
                rss_url,
                podcast_title,
                email,
                podcast_index_id or None,
                artwork_url or None,
                publisher or None,
            ),
        )
        return cur.fetchone()["id"]


def ensure_feed(
    rss_url: str,
    podcast_title: str,
    podcast_index_id: str = "",
    artwork_url: str = "",
    publisher: str = "",
    email: str = "",
) -> int:
    """Get or create a feed row without marking it active (used for on-demand summaries)."""
    owner_email = email or os.getenv("OWNER_EMAIL", "asomani@wp-labs.ai")
    with get_conn() as conn, conn.cursor() as cur:
        if podcast_index_id:
            cur.execute(
                "SELECT id FROM feeds WHERE rss_url = %s OR podcast_index_id = %s LIMIT 1",
                (rss_url, podcast_index_id),
            )
        else:
            cur.execute("SELECT id FROM feeds WHERE rss_url = %s LIMIT 1", (rss_url,))
        row = cur.fetchone()
        if row:
            return row["id"]

        cur.execute(
            """
            INSERT INTO feeds (rss_url, podcast_title, email, podcast_index_id, artwork_url, publisher, active)
            VALUES (%s, %s, %s, %s, %s, %s, FALSE)
            RETURNING id
            """,
            (
                rss_url,
                podcast_title,
                owner_email,
                podcast_index_id or None,
                artwork_url or None,
                publisher or None,
            ),
        )
        return cur.fetchone()["id"]


def list_active_feeds() -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM feeds WHERE active = TRUE")
        return cur.fetchall()


def episode_exists(feed_id: int, guid: str) -> bool:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM episodes WHERE feed_id = %s AND guid = %s",
            (feed_id, guid),
        )
        return cur.fetchone() is not None


def save_episode(
    feed_id: int,
    guid: str,
    title: str,
    published_at,
    audio_url: str,
    summary: str,
    transcript_source: str,
    mark_emailed: bool = False,
) -> int:
    emailed_at = datetime.utcnow() if mark_emailed else None
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO episodes
              (feed_id, guid, title, published_at, audio_url, summary, transcript_source, emailed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (feed_id, guid) DO UPDATE
              SET summary           = EXCLUDED.summary,
                  transcript_source = EXCLUDED.transcript_source,
                  emailed_at        = CASE
                    WHEN EXCLUDED.emailed_at IS NOT NULL THEN EXCLUDED.emailed_at
                    ELSE episodes.emailed_at
                  END
            RETURNING id
            """,
            (
                feed_id, guid, title, published_at, audio_url,
                summary, transcript_source, emailed_at,
            ),
        )
        return cur.fetchone()["id"]
