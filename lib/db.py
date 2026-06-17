"""Postgres helpers for the v5 Supabase backend.

Connects via POSTGRES_URL — the Supabase **transaction pooler** (port 6543).
Because pgbouncer transaction mode does not support prepared statements, we
disable psycopg's auto-prepare (`prepare_threshold=None`).

The backend connects as the `postgres` role, which owns these tables and so
bypasses RLS. RLS is the backstop for any anon/PostgREST access; in this code
path every user-scoped query MUST filter by the JWT-derived user_id explicitly
(see docs/PRD.md §7 and lib/auth.py).
"""
import os
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

VALID_CADENCES = {"instant", "daily", "weekly"}


def get_conn():
    url = os.getenv("POSTGRES_URL")
    if not url:
        raise RuntimeError("POSTGRES_URL not set")
    return psycopg.connect(
        url,
        row_factory=dict_row,
        prepare_threshold=None,   # required for the pgbouncer transaction pooler
        connect_timeout=10,
    )


def parse_dt(value) -> datetime | None:
    """Accept an ISO-8601 string, a unix timestamp, or a datetime; return tz-aware
    datetime or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (ValueError, OSError):
            return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# ── Global catalog: podcasts ───────────────────────────────────────────────────

def upsert_podcast(
    rss_url: str,
    title: str = "",
    publisher: str = "",
    artwork_url: str = "",
    description: str = "",
    pi_feed_id: str | None = None,
    itunes_id: str | None = None,
    categories: list[str] | None = None,
) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO podcasts
                (rss_url, title, publisher, artwork_url, description,
                 pi_feed_id, itunes_id, categories)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (rss_url) DO UPDATE SET
                title       = COALESCE(NULLIF(EXCLUDED.title, ''),       podcasts.title),
                publisher   = COALESCE(NULLIF(EXCLUDED.publisher, ''),   podcasts.publisher),
                artwork_url = COALESCE(NULLIF(EXCLUDED.artwork_url, ''), podcasts.artwork_url),
                description = COALESCE(NULLIF(EXCLUDED.description, ''), podcasts.description),
                pi_feed_id  = COALESCE(EXCLUDED.pi_feed_id,  podcasts.pi_feed_id),
                itunes_id   = COALESCE(EXCLUDED.itunes_id,   podcasts.itunes_id),
                categories  = CASE
                    WHEN EXCLUDED.categories IS NULL OR cardinality(EXCLUDED.categories) = 0
                    THEN podcasts.categories
                    ELSE EXCLUDED.categories
                END
            RETURNING id
            """,
            (rss_url, title, publisher, artwork_url, description,
             pi_feed_id, itunes_id, categories or []),
        )
        return cur.fetchone()["id"]


def get_podcast(podcast_id: int) -> dict | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM podcasts WHERE id = %s", (podcast_id,))
        return cur.fetchone()


def get_podcast_by_rss(rss_url: str) -> dict | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM podcasts WHERE rss_url = %s", (rss_url,))
        return cur.fetchone()


# ── Global catalog: episodes ───────────────────────────────────────────────────

def upsert_episode(
    podcast_id: int,
    guid: str,
    title: str = "",
    description: str = "",
    audio_url: str = "",
    episode_url: str = "",
    published_at=None,
    duration_seconds: int | None = None,
    pi_episode_id: str | None = None,
) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO episodes
                (podcast_id, guid, title, description, audio_url, episode_url,
                 published_at, duration_seconds, pi_episode_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (podcast_id, guid) DO UPDATE SET
                title            = COALESCE(NULLIF(EXCLUDED.title, ''), episodes.title),
                description      = COALESCE(NULLIF(EXCLUDED.description, ''), episodes.description),
                audio_url        = COALESCE(NULLIF(EXCLUDED.audio_url, ''), episodes.audio_url),
                episode_url      = COALESCE(NULLIF(EXCLUDED.episode_url, ''), episodes.episode_url),
                published_at     = COALESCE(EXCLUDED.published_at, episodes.published_at),
                duration_seconds = COALESCE(EXCLUDED.duration_seconds, episodes.duration_seconds),
                pi_episode_id    = COALESCE(EXCLUDED.pi_episode_id, episodes.pi_episode_id)
            RETURNING id
            """,
            (podcast_id, guid, title, description, audio_url, episode_url,
             parse_dt(published_at), duration_seconds, pi_episode_id),
        )
        return cur.fetchone()["id"]


def get_episode(podcast_id: int, guid: str) -> dict | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM episodes WHERE podcast_id = %s AND guid = %s",
            (podcast_id, guid),
        )
        return cur.fetchone()


def summarized_episode_ids(podcast_id: int, guids: list[str]) -> dict[str, int]:
    """For the given guids, return {guid: episode_id} for episodes that already
    have a cached summary — used to annotate the episode list."""
    if not guids:
        return {}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.guid, e.id
            FROM episodes e
            JOIN episode_summaries s ON s.episode_id = e.id
            WHERE e.podcast_id = %s AND e.guid = ANY(%s)
            """,
            (podcast_id, guids),
        )
        return {r["guid"]: r["id"] for r in cur.fetchall()}


# ── Global summary cache ───────────────────────────────────────────────────────

def get_episode_summary(episode_id: int) -> dict | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM episode_summaries WHERE episode_id = %s", (episode_id,))
        return cur.fetchone()


def save_episode_summary(
    episode_id: int,
    summary_md: str,
    tldr: str = "",
    target_words: int | None = None,
    transcript_source: str = "",
    model: str = "",
    style_version: str = "",
) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO episode_summaries
                (episode_id, summary_md, tldr, target_words,
                 transcript_source, model, style_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (episode_id) DO UPDATE SET
                summary_md        = EXCLUDED.summary_md,
                tldr              = EXCLUDED.tldr,
                target_words      = EXCLUDED.target_words,
                transcript_source = EXCLUDED.transcript_source,
                model             = EXCLUDED.model,
                style_version     = EXCLUDED.style_version,
                created_at        = NOW()
            RETURNING id
            """,
            (episode_id, summary_md, tldr, target_words,
             transcript_source, model, style_version),
        )
        return cur.fetchone()["id"]


# ── Profiles ───────────────────────────────────────────────────────────────────

def ensure_profile(user_id: str, email: str | None = None) -> dict:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO profiles (user_id, email)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                email = COALESCE(NULLIF(EXCLUDED.email, ''), profiles.email)
            RETURNING *
            """,
            (user_id, email),
        )
        return cur.fetchone()


def get_profile(user_id: str) -> dict | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM profiles WHERE user_id = %s", (user_id,))
        return cur.fetchone()


def update_profile(user_id: str, default_cadence: str) -> dict | None:
    if default_cadence not in VALID_CADENCES:
        raise ValueError(f"default_cadence must be one of {sorted(VALID_CADENCES)}")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE profiles SET default_cadence = %s WHERE user_id = %s RETURNING *",
            (default_cadence, user_id),
        )
        return cur.fetchone()


# ── Subscriptions ──────────────────────────────────────────────────────────────

def add_subscription(user_id: str, podcast_id: int, cadence_override: str | None = None) -> dict:
    if cadence_override is not None and cadence_override not in VALID_CADENCES:
        raise ValueError(f"cadence_override must be one of {sorted(VALID_CADENCES)}")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO subscriptions (user_id, podcast_id, cadence_override)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, podcast_id) DO UPDATE SET
                cadence_override = EXCLUDED.cadence_override
            RETURNING *
            """,
            (user_id, podcast_id, cadence_override),
        )
        return cur.fetchone()


def remove_subscription(user_id: str, podcast_id: int) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM subscriptions WHERE user_id = %s AND podcast_id = %s",
            (user_id, podcast_id),
        )


def list_subscriptions(user_id: str) -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.podcast_id, s.cadence_override, s.created_at,
                   p.title, p.publisher, p.artwork_url, p.rss_url,
                   p.pi_feed_id, p.itunes_id
            FROM subscriptions s
            JOIN podcasts p ON p.id = s.podcast_id
            WHERE s.user_id = %s
            ORDER BY s.created_at DESC
            """,
            (user_id,),
        )
        return cur.fetchall()


def subscribed_pi_feed_ids(user_id: str) -> set[str]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.pi_feed_id
            FROM subscriptions s JOIN podcasts p ON p.id = s.podcast_id
            WHERE s.user_id = %s AND p.pi_feed_id IS NOT NULL
            """,
            (user_id,),
        )
        return {r["pi_feed_id"] for r in cur.fetchall()}


# ── Usage cap (manual on-demand summaries) ─────────────────────────────────────

def count_user_summaries_today(user_id: str) -> int:
    """Manual on-demand summaries generated by a user in the last 24h, counted via
    the 'summarize' engagement event (docs/PRD.md §12, item 1)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS n FROM engagement
            WHERE user_id = %s AND action = 'summarize'
              AND created_at >= NOW() - INTERVAL '1 day'
            """,
            (user_id,),
        )
        return cur.fetchone()["n"]


def record_engagement(user_id: str, episode_id: int, action: str) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO engagement (user_id, episode_id, action) VALUES (%s, %s, %s)",
            (user_id, episode_id, action),
        )
