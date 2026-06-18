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
from lib import extract
from lib.auth import User, current_user, optional_user
from lib.notify import send_digest_email, send_summary_email
from lib.summarizer import SUMMARY_STYLE_VERSION, strip_summary_marker, summarize
from lib.transcripts import get_transcript

app = FastAPI(title="PodcastAI")

MANUAL_SUMMARY_DAILY_CAP = 4   # newly-generated, on-demand summaries per user/day
SUMMARY_MODEL = "gemini-2.5-flash"
SCAN_SECRET = os.getenv("SCAN_SECRET", "")
RSS_SCAN_WINDOW = 8            # newest N episodes per feed checked each scan tick
POPULAR_SCAN_BATCH = 8         # popular shows scanned per tick (rotating, to fit the timeout)

# Curated popular shows scanned for tracked-people appearances even when no one
# subscribes — a PE-leaning mix plus the big interview shows where notable people
# turn up. Resolved to feeds via the iTunes search at seed time.
POPULAR_SHOW_NAMES = [
    "Lex Fridman Podcast", "The Joe Rogan Experience", "Invest Like the Best",
    "Acquired", "The Tim Ferriss Show", "All-In Podcast", "Lenny's Podcast",
    "a16z Podcast", "Masters in Business", "Odd Lots", "The Knowledge Project",
    "How I Built This", "Founders", "BG2 Pod", "Dwarkesh Podcast",
    "The Diary Of A CEO", "The Twenty Minute VC", "Capital Allocators",
    "We Study Billionaires", "Business Breakdowns", "Decoder with Nilay Patel",
    "Hard Fork", "No Priors", "Stratechery",
]


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


# ── People tracking ────────────────────────────────────────────────────────────

class TrackPersonRequest(BaseModel):
    name: str


@app.get("/api/people")
def people(user: User = Depends(current_user)):
    return {"people": db.list_tracked_people(user.id)}


@app.post("/api/people")
def track_person(req: TrackPersonRequest, user: User = Depends(current_user)):
    name = (req.name or "").strip()
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Enter a person's name.")
    db.ensure_profile(user.id, user.email)
    return {"person": db.add_tracked_person(user.id, name)}


@app.delete("/api/people")
def untrack_person(person_id: int = Query(...), user: User = Depends(current_user)):
    db.remove_tracked_person(user.id, person_id)
    return {"ok": True}


# ── Topic tracking ─────────────────────────────────────────────────────────────

class TrackTopicRequest(BaseModel):
    topic: str


@app.get("/api/topics")
def topics(user: User = Depends(current_user)):
    return {"topics": db.list_tracked_topics(user.id)}


@app.post("/api/topics")
def track_topic(req: TrackTopicRequest, user: User = Depends(current_user)):
    topic = (req.topic or "").strip()
    if len(topic) < 2:
        raise HTTPException(status_code=400, detail="Enter a topic.")
    db.ensure_profile(user.id, user.email)
    return {"topic": db.add_tracked_topic(user.id, topic)}


@app.delete("/api/topics")
def untrack_topic(topic_id: int = Query(...), user: User = Depends(current_user)):
    db.remove_tracked_topic(user.id, topic_id)
    return {"ok": True}


# ── In-app reader (inbox) ──────────────────────────────────────────────────────

@app.get("/api/deliveries")
def deliveries(user: User = Depends(current_user)):
    return {"deliveries": db.list_deliveries(user.id)}


# ── Scan + delivery pipeline (called by the external scheduler) ─────────────────

@app.post("/api/scan")
def scan(x_scan_secret: str = Header(default="")):
    """Check subscribed shows + a rotating batch of popular shows for new episodes,
    summarize, and deliver — by subscription AND by tracked-person match.
    Protected by a shared secret so only the scheduler can trigger it."""
    _require_scan_secret(x_scan_secret)
    started = datetime.utcnow()
    stats = {"shows": 0, "ingested": 0, "matched": 0, "summarized": 0, "emails": 0}
    errors: list[str] = []
    tp_index = db.tracked_people_index()   # normalized name -> [(user_id, tracked_since)]
    tt_index = db.tracked_topics_index()   # [(topic_lower, user_id, tracked_since)]
    contacts: dict[str, dict] = {}         # user_id -> {email, cadence}
    targets = db.scan_targets(popular_batch=POPULAR_SCAN_BATCH)
    for podcast in targets:
        stats["shows"] += 1
        try:
            _scan_podcast(podcast, tp_index, tt_index, contacts, stats, errors)
        except Exception as e:
            errors.append(f"podcast {podcast['id']} ({podcast.get('title','?')}): {e}")
    db.mark_scanned([p["id"] for p in targets])
    run_id = db.record_scan_run(
        started, stats["shows"], stats["ingested"], stats["matched"],
        stats["summarized"], stats["emails"], errors,
    )
    return {"run_id": run_id, **stats, "errors": errors}


def _topic_matches(tracked_lower: str, ext_topics: list, ep: dict) -> bool:
    """A tracked topic matches if it relates to a main extracted topic (bidirectional
    substring) or appears in the episode title/description."""
    for t in ext_topics:
        tl = t.lower()
        if tracked_lower in tl or tl in tracked_lower:
            return True
    blob = f"{ep.get('title','')} {ep.get('description','')}".lower()
    return tracked_lower in blob


def _scan_podcast(podcast: dict, tp_index: dict, tt_index: list, contacts: dict, stats: dict, errors: list) -> None:
    episodes = catalog.episodes_from_rss(podcast["rss_url"], max_results=RSS_SCAN_WINDOW)
    subs = db.subscribers_for_podcast(podcast["id"])

    def _contact(uid: str) -> None:
        if uid not in contacts:
            c = db.profile_contact(uid)
            if c:
                contacts[uid] = {"email": c["email"], "cadence": c["cadence"]}

    for ep in episodes:
        pub = db.parse_dt(ep.get("published_at"))
        if not pub:
            continue
        # Cost guard: each episode is processed exactly once, the first time we see
        # it. (Episodes predate a just-added subscription/track, so the watermark
        # below means no back-catalog blast either.)
        if db.get_episode(podcast["id"], ep["guid"]):
            continue

        user_reasons: dict[str, list] = {}   # user_id -> [reason, ...]

        # 1. Subscription matches.
        for s in subs:
            if s.get("created_at") and s["created_at"] < pub:
                uid = str(s["user_id"])
                user_reasons.setdefault(uid, []).append({"type": "show", "podcast_id": podcast["id"]})
                contacts[uid] = {"email": s["email"], "cadence": s["cadence"]}

        # 2. Tracked person/topic matches (cheap metadata extraction, only if anyone tracks anything).
        people_rows = []
        ext_topics: list = []
        if tp_index or tt_index:
            ext = extract.extract_people_and_topics(
                podcast["title"], ep["title"], ep.get("description", ""), ep.get("persons"),
            )
            for name in ext["people"]:
                pid = db.upsert_person(name)
                people_rows.append({"person_id": pid, "confidence": 0.9, "source": "llm"})
                for uid, since in tp_index.get(db._normalize_name(name), []):
                    if since and since < pub:
                        user_reasons.setdefault(uid, []).append({"type": "person", "name": name})
                        _contact(uid)
            ext_topics = ext.get("topics", [])
            for t_lower, uid, since in tt_index:
                if since and since < pub and _topic_matches(t_lower, ext_topics, ep):
                    user_reasons.setdefault(uid, []).append({"type": "topic", "topic": t_lower})
                    _contact(uid)

        # Always ingest so we don't re-extract this episode next tick.
        episode_id = db.upsert_episode(
            podcast["id"], ep["guid"], title=ep["title"], description=ep.get("description", ""),
            audio_url=ep.get("audio_url", ""), episode_url=ep.get("episode_url", ""),
            published_at=pub, duration_seconds=ep.get("duration_seconds"),
        )
        stats["ingested"] += 1
        if people_rows:
            db.set_episode_people(episode_id, people_rows)
        if ext_topics:
            db.set_episode_topics(episode_id, ext_topics)

        if not user_reasons:
            continue  # seen + indexed, but nobody is waiting on it

        summary_md = _ensure_summary(podcast, ep, episode_id, stats, errors)
        if summary_md is None:
            continue

        for uid, reasons in user_reasons.items():
            did = db.create_delivery(uid, episode_id, reasons, status="queued")
            if did is None:
                continue  # already delivered to this user
            stats["matched"] += 1
            meta = contacts.get(uid) or {}
            if meta.get("cadence") == "instant":
                try:
                    send_summary_email(meta["email"], podcast["title"], ep["title"], summary_md)
                    stats["emails"] += 1
                except Exception as e:
                    errors.append(f"email {meta.get('email')}: {e}")
                db.mark_delivery_sent(did)


def _ensure_summary(podcast: dict, ep: dict, episode_id: int, stats: dict, errors: list) -> str | None:
    """Return the episode's summary, generating + caching it once if needed."""
    cached = db.get_episode_summary(episode_id)
    if cached and cached.get("style_version") == SUMMARY_STYLE_VERSION:
        return cached["summary_md"]
    text, source = get_transcript(
        podcast["title"], ep["title"], ep.get("description", ""),
        ep.get("audio_url", ""), ep.get("episode_url", ""),
        transcript_url=ep.get("transcript_url", ""), transcript_type=ep.get("transcript_type", ""),
    )
    if not text:
        errors.append(f"no transcript: {podcast.get('title','?')} — {ep.get('title','?')}")
        return None
    summary_md = strip_summary_marker(summarize(
        podcast["title"], ep["title"], text,
        transcript_source=source, episode_duration=ep.get("duration_seconds"),
    ))
    db.save_episode_summary(
        episode_id, summary_md=summary_md, transcript_source=source,
        model=SUMMARY_MODEL, style_version=SUMMARY_STYLE_VERSION,
    )
    stats["summarized"] += 1
    return summary_md


@app.post("/api/admin/seed-popular")
def seed_popular(x_scan_secret: str = Header(default="")):
    """One-off (idempotent): resolve the curated popular show names to feeds and
    flag them is_popular so the scan monitors them for tracked-person matches."""
    _require_scan_secret(x_scan_secret)
    seeded, errors = [], []
    for name in POPULAR_SHOW_NAMES:
        try:
            res = catalog.search_podcasts(name, max_results=1)
            if not res:
                errors.append(f"no result: {name}")
                continue
            p = res[0]
            pid = db.upsert_podcast(
                p["rss_url"], title=p["title"], publisher=p["publisher"],
                artwork_url=p["artwork"], categories=p.get("categories"),
            )
            db.set_popular(pid, True)
            seeded.append(p["title"])
        except Exception as e:
            errors.append(f"{name}: {e}")
    return {"seeded": len(seeded), "titles": seeded, "errors": errors}


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
