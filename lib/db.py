"""Postgres helpers. Uses POSTGRES_URL env var auto-injected by Vercel/Neon."""
import json
import os
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json


DEFAULT_SECTIONS = ["overview", "takeaways"]
VALID_SECTIONS = set(DEFAULT_SECTIONS)
VALID_LENGTHS = {"short", "standard", "deep"}


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


def update_feed_settings(
    feed_id: int,
    summary_length: str | None = None,
    sections: list[str] | None = None,
    frequency_days: int | None = None,
) -> dict | None:
    """Update per-feed customization. Returns the updated row or None if not found."""
    fields: list[str] = []
    values: list = []

    if summary_length is not None:
        if summary_length not in VALID_LENGTHS:
            raise ValueError(f"summary_length must be one of {sorted(VALID_LENGTHS)}")
        fields.append("summary_length = %s")
        values.append(summary_length)

    if sections is not None:
        cleaned = [s for s in sections if s in VALID_SECTIONS]
        if not cleaned:
            raise ValueError("sections must include at least one valid section")
        fields.append("sections = %s")
        values.append(cleaned)

    if frequency_days is not None:
        if frequency_days < 1 or frequency_days > 365:
            raise ValueError("frequency_days must be between 1 and 365")
        fields.append("frequency_days = %s")
        values.append(frequency_days)

    if not fields:
        return get_feed(feed_id)

    values.append(feed_id)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE feeds SET {', '.join(fields)} WHERE id = %s RETURNING *",
            tuple(values),
        )
        return cur.fetchone()


def get_feed(feed_id: int) -> dict | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM feeds WHERE id = %s", (feed_id,))
        return cur.fetchone()


def list_active_feeds() -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM feeds WHERE active = TRUE")
        return cur.fetchall()


def feed_due_for_check(feed_row: dict) -> bool:
    """True if frequency_days has elapsed since last delivery (or feed never delivered)."""
    last = feed_row.get("last_delivered_at")
    if not last:
        return True
    freq = max(int(feed_row.get("frequency_days") or 1), 1)
    delta = datetime.now(last.tzinfo) - last if last.tzinfo else datetime.utcnow() - last
    return delta.total_seconds() >= freq * 24 * 3600


def mark_feed_delivered(feed_id: int) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE feeds SET last_delivered_at = NOW() WHERE id = %s", (feed_id,))


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


def record_cron_run(
    started_at: datetime,
    feeds_checked: int,
    feeds_skipped: int,
    new_episodes: int,
    errors: list[str],
) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cron_runs
              (started_at, feeds_checked, feeds_skipped, new_episodes, errors, ok)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                started_at,
                feeds_checked,
                feeds_skipped,
                new_episodes,
                Json(errors),
                not errors,
            ),
        )
        return cur.fetchone()["id"]


def get_recent_cron_runs(limit: int = 5) -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, started_at, finished_at, feeds_checked, feeds_skipped,
                   new_episodes, errors, ok
            FROM cron_runs
            ORDER BY started_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        for r in rows:
            for k in ("started_at", "finished_at"):
                if r.get(k):
                    r[k] = r[k].isoformat()
        return rows
