"""Vercel entrypoint: a single FastAPI app serving the frontend + all API routes."""
import os
import sys
from datetime import datetime
from time import mktime
from pathlib import Path

import requests
import feedparser
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Make local imports work both locally and on Vercel
sys.path.append(str(Path(__file__).parent))

from lib.db import (
    add_feed,
    list_active_feeds,
    episode_exists,
    save_episode,
    get_conn,
)
from lib.transcripts import get_transcript
from lib.summarizer import summarize
from lib.notify import send_summary_email

app = FastAPI(title="Podcast Summary Agent")

# --- Static frontend ---
PUBLIC_DIR = Path(__file__).parent / "public"
if (PUBLIC_DIR / "static").exists():
    app.mount("/static", StaticFiles(directory=str(PUBLIC_DIR / "static")), name="static")


@app.get("/")
def home():
    return FileResponse(str(PUBLIC_DIR / "index.html"))


# --- API ---
class SubscribeRequest(BaseModel):
    rss_url: str
    email: str


@app.post("/api/subscribe")
def subscribe(req: SubscribeRequest):
    try:
        resp = requests.get(
            req.rss_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
            verify=False,
        )
        feed = feedparser.parse(resp.content)
        podcast_title = feed.feed.get("title", "Unknown Podcast")
        feed_id = add_feed(req.rss_url, podcast_title, req.email)
        return {"id": feed_id, "podcast_title": podcast_title}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/feeds")
def feeds():
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.id, f.rss_url, f.podcast_title, f.email, f.created_at,
                       COUNT(e.id) AS episode_count
                FROM feeds f
                LEFT JOIN episodes e ON e.feed_id = f.id
                WHERE f.active = TRUE
                GROUP BY f.id
                ORDER BY f.created_at DESC
                """
            )
            rows = cur.fetchall()
            for r in rows:
                if r.get("created_at"):
                    r["created_at"] = r["created_at"].isoformat()
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
        return {"episodes": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/cron-check")
def cron_check():
    """Vercel Cron entry point: scan feeds, summarize new episodes, email them."""
    results = {"feeds_checked": 0, "new_episodes": 0, "errors": []}

    for feed_row in list_active_feeds():
        results["feeds_checked"] += 1
        try:
            _process_feed(feed_row, results)
        except Exception as e:
            results["errors"].append(f"feed {feed_row['id']}: {e}")

    return results


def _process_feed(feed_row: dict, results: dict):
    resp = requests.get(
        feed_row["rss_url"],
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
        verify=False,
    )
    feed = feedparser.parse(resp.content)
    podcast_title = feed.feed.get("title", feed_row["podcast_title"])

    for entry in feed.entries[:5]:
        guid = entry.get("id") or entry.get("link") or entry.get("title")
        if not guid or episode_exists(feed_row["id"], guid):
            continue

        title = entry.get("title", "Untitled")
        description = entry.get("summary", entry.get("description", ""))
        audio_url = ""
        for enc in entry.get("enclosures", []):
            if "audio" in enc.get("type", ""):
                audio_url = enc.get("href", "")
                break

        published_at = None
        if entry.get("published_parsed"):
            published_at = datetime.fromtimestamp(mktime(entry.published_parsed))

        text, source = get_transcript(podcast_title, title, description)
        if not text:
            continue

        summary = summarize(podcast_title, title, text)
        send_summary_email(feed_row["email"], podcast_title, title, summary)
        save_episode(
            feed_row["id"], guid, title, published_at, audio_url, summary, source
        )
        results["new_episodes"] += 1
