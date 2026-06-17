"""Vercel entrypoint — single FastAPI app serving the frontend + all API routes.

v5: multi-tenant. Auth via Supabase (lib.auth), data in Supabase Postgres
(lib.db), catalog/search via Podcast Index (lib.podcastindex), summaries cached
once-per-episode and reused across users (fixed PE lens). See docs/PRD.md.
"""
import os
import sys
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

sys.path.append(str(Path(__file__).parent))

from lib import db
from lib import podcastindex as pi
from lib.auth import User, current_user, optional_user
from lib.podcastindex import PodcastIndexError
from lib.summarizer import SUMMARY_STYLE_VERSION, strip_summary_marker, summarize
from lib.transcripts import get_transcript

app = FastAPI(title="PodcastAI")

MANUAL_SUMMARY_DAILY_CAP = 4   # newly-generated, on-demand summaries per user/day
SUMMARY_MODEL = "gemini-2.5-flash"


# ── Static redirect ────────────────────────────────────────────────────────────

@app.get("/")
def home():
    return RedirectResponse(url="/index.html", status_code=302)


# ── Public config for the frontend (supabase-js init) ───────────────────────────

@app.get("/api/config")
def config():
    url = os.getenv("SUPABASE_URL")
    anon = os.getenv("SUPABASE_ANON_KEY")
    if not url or not anon:
        raise HTTPException(status_code=500, detail="Supabase env vars not configured.")
    return {"supabase_url": url, "supabase_anon_key": anon}


# ── Account ──────────────────────────────────────────────────────────────────

@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return {"profile": db.ensure_profile(user.id, user.email)}


class ProfileUpdate(BaseModel):
    default_cadence: str


@app.patch("/api/me")
def update_me(req: ProfileUpdate, user: User = Depends(current_user)):
    try:
        db.ensure_profile(user.id, user.email)
        return {"profile": db.update_profile(user.id, req.default_cadence)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Podcast search ─────────────────────────────────────────────────────────────

@app.get("/api/search")
def search_podcasts(q: str = Query(..., min_length=1), user: User | None = Depends(optional_user)):
    try:
        results = pi.search_podcasts(q, max_results=20)
    except PodcastIndexError as e:
        raise HTTPException(status_code=503, detail=f"Search unavailable: {e}")

    subscribed = db.subscribed_pi_feed_ids(user.id) if user else set()
    for r in results:
        r["subscribed"] = bool(r.get("pi_feed_id") and r["pi_feed_id"] in subscribed)
    return {"podcasts": results}


# ── Episode list for a podcast (+ optional back-catalog search) ────────────────

@app.get("/api/podcast-episodes")
def podcast_episodes(
    pi_feed_id: str = Query(default=""),
    rss_url: str = Query(default=""),
    q: str = Query(default=""),
    max_results: int = Query(default=50, le=200),
):
    if not pi_feed_id and not rss_url:
        raise HTTPException(status_code=400, detail="pi_feed_id or rss_url required.")
    try:
        if q:
            episodes = pi.search_episodes_in_feed(pi_feed_id or rss_url, q, max_results=max_results)
        elif pi_feed_id:
            episodes = pi.episodes_by_feed_id(pi_feed_id, max_results=max_results)
        else:
            episodes = pi.episodes_by_feed_url(rss_url, max_results=max_results)
    except PodcastIndexError as e:
        raise HTTPException(status_code=503, detail=f"Episodes unavailable: {e}")

    # Annotate which episodes already have a cached summary.
    podcast = db.get_podcast_by_rss(rss_url) if rss_url else None
    summarized: dict[str, int] = {}
    if podcast:
        summarized = db.summarized_episode_ids(podcast["id"], [e["guid"] for e in episodes])
    for e in episodes:
        e["episode_id"] = summarized.get(e["guid"])
        e["has_summary"] = e["guid"] in summarized
    return {"episodes": episodes}


# ── On-demand summary ──────────────────────────────────────────────────────────

class SummarizeRequest(BaseModel):
    rss_url: str
    pi_feed_id: str = ""
    podcast_title: str = ""
    artwork_url: str = ""
    publisher: str = ""
    categories: list[str] = []
    episode_guid: str
    episode_title: str = ""
    episode_description: str = ""
    episode_audio_url: str = ""
    episode_url: str = ""
    episode_published_at: str | None = None
    episode_duration_seconds: int | None = None


@app.post("/api/summarize")
def summarize_episode(req: SummarizeRequest, user: User = Depends(current_user)):
    db.ensure_profile(user.id, user.email)

    podcast_id = db.upsert_podcast(
        req.rss_url, title=req.podcast_title, publisher=req.publisher,
        artwork_url=req.artwork_url, pi_feed_id=req.pi_feed_id or None,
        categories=req.categories or None,
    )
    episode_id = db.upsert_episode(
        podcast_id, req.episode_guid, title=req.episode_title,
        description=req.episode_description, audio_url=req.episode_audio_url,
        episode_url=req.episode_url, published_at=req.episode_published_at,
        duration_seconds=req.episode_duration_seconds, pi_episode_id=None,
    )

    cached = db.get_episode_summary(episode_id)
    if cached and cached.get("style_version") == SUMMARY_STYLE_VERSION:
        return {
            "episode_id": episode_id,
            "summary": cached["summary_md"],
            "source": cached.get("transcript_source"),
            "cached": True,
        }

    # Enforce the per-user daily cap on newly-generated summaries.
    if db.count_user_summaries_today(user.id) >= MANUAL_SUMMARY_DAILY_CAP:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Daily limit of {MANUAL_SUMMARY_DAILY_CAP} on-demand summaries reached. "
                "Subscriptions and tracked people/topics still deliver automatically."
            ),
        )

    text, source = get_transcript(
        req.podcast_title, req.episode_title, req.episode_description,
        req.episode_audio_url, req.episode_url,
    )
    if not text:
        raise HTTPException(
            status_code=422,
            detail="Could not extract a transcript for this episode. Try a different one.",
        )

    marked = summarize(
        req.podcast_title, req.episode_title, text,
        transcript_source=source, episode_duration=req.episode_duration_seconds,
    )
    summary_md = strip_summary_marker(marked)

    db.save_episode_summary(
        episode_id, summary_md=summary_md, target_words=None,
        transcript_source=source, model=SUMMARY_MODEL, style_version=SUMMARY_STYLE_VERSION,
    )
    db.record_engagement(user.id, episode_id, "summarize")

    return {"episode_id": episode_id, "summary": summary_md, "source": source, "cached": False}


# ── Subscriptions ──────────────────────────────────────────────────────────────

class SubscribeRequest(BaseModel):
    rss_url: str
    pi_feed_id: str = ""
    podcast_title: str = ""
    artwork_url: str = ""
    publisher: str = ""
    categories: list[str] = []
    cadence_override: str | None = None


@app.post("/api/subscribe")
def subscribe(req: SubscribeRequest, user: User = Depends(current_user)):
    db.ensure_profile(user.id, user.email)
    try:
        podcast_id = db.upsert_podcast(
            req.rss_url, title=req.podcast_title, publisher=req.publisher,
            artwork_url=req.artwork_url, pi_feed_id=req.pi_feed_id or None,
            categories=req.categories or None,
        )
        sub = db.add_subscription(user.id, podcast_id, cadence_override=req.cadence_override)
        return {"subscription": sub, "podcast_id": podcast_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/subscriptions")
def subscriptions(user: User = Depends(current_user)):
    return {"subscriptions": db.list_subscriptions(user.id)}


@app.delete("/api/subscriptions")
def unsubscribe(podcast_id: int = Query(...), user: User = Depends(current_user)):
    db.remove_subscription(user.id, podcast_id)
    return {"ok": True}


class SubscriptionCadenceUpdate(BaseModel):
    cadence_override: str | None = None


@app.patch("/api/subscriptions/{podcast_id}")
def update_subscription(
    podcast_id: int, req: SubscriptionCadenceUpdate, user: User = Depends(current_user)
):
    try:
        sub = db.add_subscription(user.id, podcast_id, cadence_override=req.cadence_override)
        return {"subscription": sub}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
