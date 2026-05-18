"""Vercel entrypoint — single FastAPI app serving the frontend + all API routes."""
import os
import sys
from datetime import datetime
from pathlib import Path
from time import mktime

import feedparser
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

sys.path.append(str(Path(__file__).parent))

from lib.db import (
    DEFAULT_SECTIONS,
    add_feed,
    ensure_feed,
    episode_exists,
    feed_due_for_check,
    get_conn,
    get_recent_cron_runs,
    list_active_feeds,
    mark_feed_delivered,
    record_cron_run,
    save_episode,
    update_feed_settings,
)
from lib.notify import send_summary_email
from lib.summarizer import strip_summary_marker, summarize, summary_is_current
from lib.transcripts import get_transcript

app = FastAPI(title="PodcastAI")

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "asomani@wp-labs.ai")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _rss_fetch(url: str) -> bytes:
    resp = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
        verify=False,
    )
    resp.raise_for_status()
    return resp.content


def _serialize_feed_row(r: dict) -> dict:
    out = dict(r)
    for k in ("created_at", "last_delivered_at"):
        if out.get(k):
            out[k] = out[k].isoformat()
    if out.get("sections") is None:
        out["sections"] = list(DEFAULT_SECTIONS)
    return out


# ── Static redirect ────────────────────────────────────────────────────────────

@app.get("/")
def home():
    return RedirectResponse(url="/index.html", status_code=302)


# ── Podcast search ─────────────────────────────────────────────────────────────

@app.get("/api/search")
def search_podcasts(q: str = Query(..., min_length=1)):
    """Search podcasts via the (unauthenticated) Apple iTunes Search API.
    Returns title / publisher / artwork / RSS feed URL — the same shape the
    rest of the app already consumes for Podcast Index results."""
    try:
        r = requests.get(
            ITUNES_SEARCH_URL,
            params={
                "term":   q,
                "media":  "podcast",
                "entity": "podcast",
                "limit":  12,
            },
            headers={"User-Agent": "PodcastAI/3.0"},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results", [])

        # The `podcast_index_id` column doubles as a generic external-id store;
        # for iTunes results we use `collectionId` (Apple's podcast id).
        subscribed_ids: set[str] = set()
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT podcast_index_id FROM feeds WHERE active = TRUE AND podcast_index_id IS NOT NULL"
                )
                subscribed_ids = {row["podcast_index_id"] for row in cur.fetchall()}
        except Exception:
            pass

        podcasts = []
        seen: set[str] = set()
        for f in results:
            pid = str(f.get("collectionId", "") or f.get("trackId", ""))
            feed_url = f.get("feedUrl", "")
            if not feed_url:
                continue  # podcast without a public feed isn't useful to us
            # iTunes can return the same podcast from multiple country storefronts;
            # dedupe on feed URL (collectionId differs across storefronts).
            dedup_key = feed_url.lower().rstrip("/")
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            artwork = (
                f.get("artworkUrl600")
                or f.get("artworkUrl100")
                or f.get("artworkUrl60")
                or ""
            )
            podcasts.append({
                "id":            pid,
                "title":         f.get("collectionName") or f.get("trackName") or "",
                "publisher":     f.get("artistName", ""),
                "artwork":       artwork,
                "rss_url":       feed_url,
                "episode_count": f.get("trackCount", 0),
                "description":   f.get("primaryGenreName", "") or "",
                "subscribed":    pid in subscribed_ids,
            })

        return {"podcasts": podcasts}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Episode list for a podcast ─────────────────────────────────────────────────

@app.get("/api/podcast-episodes")
def podcast_episodes(
    podcast_index_id: str = Query(...),
    rss_url: str = Query(...),
):
    try:
        feed = feedparser.parse(_rss_fetch(rss_url))
        entries = feed.entries[:10]

        guids = [
            e.get("id") or e.get("link") or e.get("title", "")
            for e in entries
        ]

        cached: dict[str, dict] = {}
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM feeds WHERE podcast_index_id = %s OR rss_url = %s LIMIT 1",
                    (podcast_index_id, rss_url),
                )
                row = cur.fetchone()
                if row:
                    cur.execute(
                        """
                        SELECT id, guid, summary, transcript_source
                        FROM episodes
                        WHERE feed_id = %s AND guid = ANY(%s)
                        """,
                        (row["id"], guids),
                    )
                    for r in cur.fetchall():
                        cached[r["guid"]] = r
        except Exception:
            pass

        episodes = []
        for entry in entries:
            guid = entry.get("id") or entry.get("link") or entry.get("title", "")
            audio_url = ""
            for enc in entry.get("enclosures", []):
                if "audio" in enc.get("type", ""):
                    audio_url = enc.get("href", "")
                    break

            pub = None
            if entry.get("published_parsed"):
                pub = datetime.fromtimestamp(mktime(entry.published_parsed)).isoformat()

            ep = cached.get(guid)
            has_current_summary = bool(ep and summary_is_current(ep.get("summary")))
            description = entry.get("summary") or entry.get("description") or ""
            episodes.append({
                "guid":              guid,
                "title":             entry.get("title", "Untitled"),
                "published_at":      pub,
                "episode_url":       entry.get("link", ""),
                "audio_url":         audio_url,
                "description":       description[:12000],
                "duration":          entry.get("itunes_duration", ""),
                "has_summary":       has_current_summary,
                "episode_id":        ep["id"] if has_current_summary else None,
                "summary":           strip_summary_marker(ep["summary"]) if has_current_summary else None,
                "transcript_source": ep["transcript_source"] if ep else None,
            })

        return {"episodes": episodes, "podcast_title": feed.feed.get("title", "")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── On-demand summary ──────────────────────────────────────────────────────────

class SummarizeRequest(BaseModel):
    podcast_index_id:    str = ""
    rss_url:             str
    podcast_title:       str
    artwork_url:         str = ""
    publisher:           str = ""
    episode_guid:        str
    episode_title:       str
    episode_url:         str = ""
    episode_audio_url:   str = ""
    episode_description: str = ""
    episode_duration:    str = ""
    episode_published_at: str | None = None


@app.post("/api/summarize")
def summarize_episode(req: SummarizeRequest):
    try:
        feed_id = ensure_feed(
            req.rss_url,
            req.podcast_title,
            req.podcast_index_id,
            req.artwork_url,
            req.publisher,
        )

        # Look up this feed's customization knobs so on-demand summaries match
        # what cron-driven summaries would produce.
        feed_settings = {"summary_length": "standard", "sections": list(DEFAULT_SECTIONS)}
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT summary_length, sections FROM feeds WHERE id = %s",
                    (feed_id,),
                )
                row = cur.fetchone()
                if row:
                    feed_settings["summary_length"] = row.get("summary_length") or "standard"
                    feed_settings["sections"] = row.get("sections") or list(DEFAULT_SECTIONS)
        except Exception:
            pass

        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, summary, transcript_source FROM episodes WHERE feed_id = %s AND guid = %s",
                (feed_id, req.episode_guid),
            )
            cached = cur.fetchone()

        if cached and summary_is_current(cached["summary"]):
            return {
                "episode_id": cached["id"],
                "summary":    strip_summary_marker(cached["summary"]),
                "source":     cached["transcript_source"],
                "cached":     True,
            }

        text, source = get_transcript(
            req.podcast_title,
            req.episode_title,
            req.episode_description,
            req.episode_audio_url,
            req.episode_url,
        )
        if not text:
            raise HTTPException(
                status_code=422,
                detail="Could not extract a transcript for this episode. Try a different one.",
            )

        summary_text = summarize(
            req.podcast_title,
            req.episode_title,
            text,
            length=feed_settings["summary_length"],
            sections=feed_settings["sections"],
            transcript_source=source,
            episode_duration=req.episode_duration,
        )

        pub = None
        if req.episode_published_at:
            try:
                pub = datetime.fromisoformat(req.episode_published_at)
            except ValueError:
                pass

        ep_id = save_episode(
            feed_id,
            req.episode_guid,
            req.episode_title,
            pub,
            req.episode_audio_url,
            summary_text,
            source,
            mark_emailed=False,
        )

        return {
            "episode_id": ep_id,
            "summary": strip_summary_marker(summary_text),
            "source": source,
            "cached": False,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Email a stored summary ─────────────────────────────────────────────────────

class EmailRequest(BaseModel):
    episode_id: int


@app.post("/api/email-summary")
def email_episode_summary(req: EmailRequest):
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.title, e.summary, f.podcast_title, f.email
                FROM episodes e
                JOIN feeds f ON f.id = e.feed_id
                WHERE e.id = %s
                """,
                (req.episode_id,),
            )
            row = cur.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Episode not found.")

        send_summary_email(
            row["email"] or OWNER_EMAIL,
            row["podcast_title"],
            row["title"],
            strip_summary_marker(row["summary"]),
        )

        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE episodes SET emailed_at = NOW() WHERE id = %s",
                (req.episode_id,),
            )

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Subscription management ────────────────────────────────────────────────────

class SubscribeRequest(BaseModel):
    rss_url:          str
    email:            str = ""
    podcast_index_id: str = ""
    artwork_url:      str = ""
    publisher:        str = ""
    podcast_title:    str = ""


@app.post("/api/subscribe")
def subscribe(req: SubscribeRequest):
    try:
        email = req.email or OWNER_EMAIL
        if req.podcast_title:
            podcast_title = req.podcast_title
        else:
            feed = feedparser.parse(_rss_fetch(req.rss_url))
            podcast_title = feed.feed.get("title", "Unknown Podcast")

        feed_id = add_feed(
            req.rss_url,
            podcast_title,
            email,
            req.podcast_index_id,
            req.artwork_url,
            req.publisher,
        )
        return {"id": feed_id, "podcast_title": podcast_title}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/feeds")
def feeds():
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.id, f.rss_url, f.podcast_title, f.email,
                       f.podcast_index_id, f.artwork_url, f.publisher,
                       f.summary_length, f.sections, f.frequency_days,
                       f.last_delivered_at, f.created_at,
                       COUNT(e.id) AS episode_count
                FROM feeds f
                LEFT JOIN episodes e ON e.feed_id = f.id
                WHERE f.active = TRUE
                GROUP BY f.id
                ORDER BY f.created_at DESC
                """
            )
            rows = [_serialize_feed_row(r) for r in cur.fetchall()]
        return {"feeds": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/feeds")
def unsubscribe(id: int = Query(...)):
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE feeds SET active = FALSE WHERE id = %s", (id,))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class FeedSettingsRequest(BaseModel):
    summary_length: str | None = None
    sections:       list[str] | None = None
    frequency_days: int | None = None


@app.patch("/api/feeds/{feed_id}")
def update_feed(feed_id: int, req: FeedSettingsRequest):
    try:
        row = update_feed_settings(
            feed_id,
            summary_length=req.summary_length,
            sections=req.sections,
            frequency_days=req.frequency_days,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Feed not found.")
        return {"feed": _serialize_feed_row(row)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/episodes")
def episodes(feed_id: int | None = None):
    try:
        with get_conn() as conn, conn.cursor() as cur:
            if feed_id:
                cur.execute(
                    """
                    SELECT e.*, f.podcast_title FROM episodes e
                    JOIN feeds f ON f.id = e.feed_id
                    WHERE e.feed_id = %s
                    ORDER BY e.published_at DESC NULLS LAST LIMIT 50
                    """,
                    (feed_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT e.*, f.podcast_title FROM episodes e
                    JOIN feeds f ON f.id = e.feed_id
                    ORDER BY e.published_at DESC NULLS LAST LIMIT 50
                    """
                )
            rows = cur.fetchall()
            for r in rows:
                for k in ("published_at", "emailed_at", "created_at"):
                    if r.get(k):
                        r[k] = r[k].isoformat()
                if r.get("summary"):
                    r["summary"] = strip_summary_marker(r["summary"])
        return {"episodes": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Status (recent cron runs) ──────────────────────────────────────────────────

@app.get("/api/status")
def status():
    try:
        runs = get_recent_cron_runs(limit=5)
        last = runs[0] if runs else None
        return {"last_run": last, "recent_runs": runs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Cron ───────────────────────────────────────────────────────────────────────

@app.get("/api/cron-check")
def cron_check():
    started_at = datetime.utcnow()
    results = {
        "feeds_checked": 0,
        "feeds_skipped": 0,
        "new_episodes":  0,
        "errors":        [],
    }
    for feed_row in list_active_feeds():
        if not feed_due_for_check(feed_row):
            results["feeds_skipped"] += 1
            continue
        results["feeds_checked"] += 1
        try:
            _process_feed(feed_row, results)
        except Exception as e:
            results["errors"].append(f"feed {feed_row['id']} ({feed_row.get('podcast_title','?')}): {e}")

    try:
        run_id = record_cron_run(
            started_at,
            feeds_checked=results["feeds_checked"],
            feeds_skipped=results["feeds_skipped"],
            new_episodes=results["new_episodes"],
            errors=results["errors"],
        )
        results["run_id"] = run_id
    except Exception as e:
        results["errors"].append(f"cron-log: {e}")

    return results


def _process_feed(feed_row: dict, results: dict):
    feed = feedparser.parse(_rss_fetch(feed_row["rss_url"]))
    podcast_title = feed.feed.get("title", feed_row["podcast_title"])
    length = feed_row.get("summary_length") or "standard"
    sections = feed_row.get("sections") or list(DEFAULT_SECTIONS)

    delivered_any = False
    for entry in feed.entries[:5]:
        guid = entry.get("id") or entry.get("link") or entry.get("title")
        if not guid or episode_exists(feed_row["id"], guid):
            continue

        title = entry.get("title", "Untitled")
        description = entry.get("summary", entry.get("description", ""))
        episode_url = entry.get("link", "")
        duration = entry.get("itunes_duration", "")
        audio_url = ""
        for enc in entry.get("enclosures", []):
            if "audio" in enc.get("type", ""):
                audio_url = enc.get("href", "")
                break

        pub = None
        if entry.get("published_parsed"):
            pub = datetime.fromtimestamp(mktime(entry.published_parsed))

        text, source = get_transcript(podcast_title, title, description, audio_url, episode_url)
        if not text:
            continue

        summary_text = summarize(
            podcast_title,
            title,
            text,
            length=length,
            sections=sections,
            transcript_source=source,
            episode_duration=duration,
        )
        send_summary_email(
            feed_row["email"],
            podcast_title,
            title,
            strip_summary_marker(summary_text),
        )
        save_episode(
            feed_row["id"], guid, title, pub, audio_url,
            summary_text, source, mark_emailed=True,
        )
        results["new_episodes"] += 1
        delivered_any = True

    if delivered_any:
        mark_feed_delivered(feed_row["id"])
