"""Vercel entrypoint — single FastAPI app serving the frontend + all API routes.

v5: multi-tenant. Auth via Supabase (lib.auth), data in Supabase Postgres
(lib.db), catalog/search via Podcast Index (lib.podcastindex), summaries cached
once-per-episode and reused across users (fixed PE lens). See docs/PRD.md.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

sys.path.append(str(Path(__file__).parent))

from lib import catalog
from lib import db
from lib.auth import User, current_user, optional_user
from lib.notify import send_digest_email, send_summary_email
from lib.summarizer import SUMMARY_STYLE_VERSION, strip_summary_marker, summarize
from lib.transcripts import get_transcript

app = FastAPI(title="PodcastAI")

MANUAL_SUMMARY_DAILY_CAP = 4   # newly-generated, on-demand summaries per user/day
SUMMARY_MODEL = "gemini-2.5-flash"
SCAN_SECRET = os.getenv("SCAN_SECRET", "")
RSS_SCAN_WINDOW = 8            # newest N episodes per feed checked each scan tick


def _require_scan_secret(secret: str) -> None:
    if not SCAN_SECRET or secret != SCAN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized.")


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
        results = catalog.search_podcasts(q, max_results=20)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Search unavailable: {e}")

    subscribed = db.subscribed_rss_urls(user.id) if user else set()
    for r in results:
        rss = (r.get("rss_url") or "").lower().rstrip("/")
        r["subscribed"] = bool(rss and rss in subscribed)
    return {"podcasts": results}


# ── Episode list for a podcast (+ optional back-catalog search) ────────────────

@app.get("/api/podcast-episodes")
def podcast_episodes(
    rss_url: str = Query(default=""),
    pi_feed_id: str = Query(default=""),   # accepted for forward-compat; unused in catalog mode
    q: str = Query(default=""),
    max_results: int = Query(default=50, le=200),
):
    if not rss_url:
        raise HTTPException(status_code=400, detail="rss_url required.")
    try:
        episodes = catalog.episodes_from_rss(rss_url, max_results=max_results, query=q)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not load episodes: {e}")

    # Annotate which episodes already have a cached summary.
    podcast = db.get_podcast_by_rss(rss_url)
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
    episode_transcript_url: str = ""
    episode_transcript_type: str = ""


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
        transcript_url=req.episode_transcript_url,
        transcript_type=req.episode_transcript_type,
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


# ── In-app reader (inbox) ──────────────────────────────────────────────────────

@app.get("/api/deliveries")
def deliveries(user: User = Depends(current_user)):
    return {"deliveries": db.list_deliveries(user.id)}


# ── Scan + delivery pipeline (called by the external scheduler) ─────────────────

@app.post("/api/scan")
def scan(x_scan_secret: str = Header(default="")):
    """Check every subscribed podcast for new episodes, summarize, and deliver.
    Protected by a shared secret so only the scheduler can trigger it."""
    _require_scan_secret(x_scan_secret)
    started = datetime.utcnow()
    stats = {"shows": 0, "ingested": 0, "matched": 0, "summarized": 0, "emails": 0}
    errors: list[str] = []
    for podcast in db.distinct_subscribed_podcasts():
        stats["shows"] += 1
        try:
            _scan_podcast(podcast, stats, errors)
        except Exception as e:
            errors.append(f"podcast {podcast['id']} ({podcast.get('title','?')}): {e}")
    run_id = db.record_scan_run(
        started, stats["shows"], stats["ingested"], stats["matched"],
        stats["summarized"], stats["emails"], errors,
    )
    return {"run_id": run_id, **stats, "errors": errors}


def _scan_podcast(podcast: dict, stats: dict, errors: list) -> None:
    episodes = catalog.episodes_from_rss(podcast["rss_url"], max_results=RSS_SCAN_WINDOW)
    subs = db.subscribers_for_podcast(podcast["id"])
    if not subs:
        return

    for ep in episodes:
        pub = db.parse_dt(ep.get("published_at"))
        if not pub:
            continue
        # Only deliver episodes published after a user subscribed (no back-catalog blast).
        eligible = [s for s in subs if s.get("created_at") and s["created_at"] < pub]
        if not eligible:
            continue

        episode_id = db.upsert_episode(
            podcast["id"], ep["guid"], title=ep["title"], description=ep["description"],
            audio_url=ep["audio_url"], episode_url=ep["episode_url"],
            published_at=pub, duration_seconds=ep.get("duration_seconds"),
        )
        stats["ingested"] += 1

        # Summarize once per episode (shared cache across all users).
        cached = db.get_episode_summary(episode_id)
        if cached and cached.get("style_version") == SUMMARY_STYLE_VERSION:
            summary_md = cached["summary_md"]
        else:
            text, source = get_transcript(
                podcast["title"], ep["title"], ep["description"],
                ep["audio_url"], ep["episode_url"],
                transcript_url=ep.get("transcript_url", ""),
                transcript_type=ep.get("transcript_type", ""),
            )
            if not text:
                errors.append(f"no transcript: {podcast.get('title','?')} — {ep.get('title','?')}")
                continue
            summary_md = strip_summary_marker(summarize(
                podcast["title"], ep["title"], text,
                transcript_source=source, episode_duration=ep.get("duration_seconds"),
            ))
            db.save_episode_summary(
                episode_id, summary_md=summary_md, transcript_source=source,
                model=SUMMARY_MODEL, style_version=SUMMARY_STYLE_VERSION,
            )
            stats["summarized"] += 1

        for s in eligible:
            did = db.create_delivery(
                s["user_id"], episode_id,
                [{"type": "show", "podcast_id": podcast["id"]}], status="queued",
            )
            if did is None:
                continue  # already delivered to this user
            stats["matched"] += 1
            if s["cadence"] == "instant":
                try:
                    send_summary_email(s["email"], podcast["title"], ep["title"], summary_md)
                    stats["emails"] += 1
                except Exception as e:
                    errors.append(f"email {s['email']}: {e}")
                db.mark_delivery_sent(did)


@app.post("/api/digest")
def digest(cadence: str = Query("daily"), x_scan_secret: str = Header(default="")):
    """Send batched digests for a cadence ('daily'|'weekly'). Marks deliveries sent
    even if the email can't go out yet (the in-app reader always has them)."""
    _require_scan_secret(x_scan_secret)
    if cadence not in ("daily", "weekly"):
        raise HTTPException(status_code=400, detail="cadence must be 'daily' or 'weekly'.")

    by_user: dict[str, dict] = {}
    for r in db.queued_deliveries(cadence):
        u = by_user.setdefault(r["user_id"], {"email": r["email"], "items": [], "ids": []})
        u["items"].append(r)
        u["ids"].append(r["id"])

    sent, errors = 0, []
    for data in by_user.values():
        try:
            send_digest_email(data["email"], data["items"])
            sent += 1
        except Exception as e:
            errors.append(f"digest {data['email']}: {e}")
        for did in data["ids"]:
            db.mark_delivery_sent(did)
    return {"users": len(by_user), "emails_sent": sent, "errors": errors}


# ── Scan status (for the in-app banner) ────────────────────────────────────────

@app.get("/api/status")
def status():
    runs = db.get_recent_scan_runs(limit=5)
    return {"last_run": runs[0] if runs else None, "recent_runs": runs}
