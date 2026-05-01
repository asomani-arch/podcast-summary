"""Vercel Postgres helper. Uses the POSTGRES_URL env var auto-injected by Vercel."""
import os
import psycopg
from psycopg.rows import dict_row


def get_conn():
    """Return a new Postgres connection. Caller is responsible for closing."""
    url = os.getenv("POSTGRES_URL")
    if not url:
        raise RuntimeError("POSTGRES_URL not set")
    return psycopg.connect(url, row_factory=dict_row)


def add_feed(rss_url: str, podcast_title: str, email: str) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO feeds (rss_url, podcast_title, email)
               VALUES (%s, %s, %s)
               ON CONFLICT (rss_url) DO UPDATE SET active = TRUE
               RETURNING id""",
            (rss_url, podcast_title, email),
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
) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO episodes
               (feed_id, guid, title, published_at, audio_url, summary,
                transcript_source, emailed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
               RETURNING id""",
            (feed_id, guid, title, published_at, audio_url, summary, transcript_source),
        )
        return cur.fetchone()["id"]
