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
    # Broader interview / behavioral-science shows where notable guests also turn up,
    # so people-tracking isn't limited to the business/tech canon.
    "A Slight Change of Plans", "The Mel Robbins Podcast", "ReThinking with Adam Grant",
    "Huberman Lab", "Armchair Expert with Dax Shepard", "Freakonomics Radio",
    "On Purpose with Jay Shetty", "The Ezra Klein Show",
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
    default_cadence: str | None = None
    summary_detail: str | None = None


@app.patch("/api/me")
def update_me(req: ProfileUpdate, user: User = Depends(current_user)):
    try:
        db.ensure_profile(user.id, user.email)
        return {"profile": db.update_profile(
            user.id,
            default_cadence=req.default_cadence,
            summary_detail=req.summary_detail,
        )}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/me/export")
def export_me(user: User = Depends(current_user)):
    """Let a user download everything we hold for them (privacy promise in the
    sign-in fine print)."""
    db.ensure_profile(user.id, user.email)
    return {
        "exported_at":   datetime.utcnow().isoformat() + "Z",
        "profile":       db.get_profile(user.id),
        "subscriptions": db.list_subscriptions(user.id),
        "people":        db.list_tracked_people(user.id),
        "topics":        db.list_tracked_topics(user.id),
        "deliveries":    db.list_deliveries(user.id, limit=500),
    }


@app.delete("/api/me")
def delete_me(user: User = Depends(current_user)):
    """Permanently delete the user's auth account; FK ON DELETE CASCADE removes all
    their rows (profile, subscriptions, tracked people/topics, deliveries)."""
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    base = os.getenv("SUPABASE_URL")
    if not service_key or not base:
        raise HTTPException(
            status_code=500,
            detail="Account deletion isn't configured. Contact support to remove your data.",
        )
    import requests
    try:
        resp = requests.delete(
            f"{base.rstrip('/')}/auth/v1/admin/users/{user.id}",
            headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
            timeout=10,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not reach the auth service: {e}")
    if resp.status_code not in (200, 204):
        raise HTTPException(status_code=502, detail="Could not delete the account. Try again.")
    return {"ok": True}


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


# ── Discovery surfaces (warm the cold start) ───────────────────────────────────

@app.get("/api/popular")
def popular_podcasts(user: User | None = Depends(optional_user)):
    """Curated popular shows for the empty home, so a new user has something to
    browse before searching."""
    results = db.list_popular_podcasts(limit=24)
    subscribed = db.subscribed_rss_urls(user.id) if user else set()
    for r in results:
        rss = (r.get("rss_url") or "").lower().rstrip("/")
        r["subscribed"] = bool(rss and rss in subscribed)
    return {"podcasts": results}


@app.get("/api/sample-summary")
def sample_summary():
    """One real, recently-generated summary to show a new user what they'll get."""
    row = db.latest_public_summary()
    if not row:
        return {"sample": None}
    return {"sample": {
        "episode_id":    row["episode_id"],
        "episode_title": row.get("episode_title") or "",
        "podcast_title": row.get("podcast_title") or "",
        "artwork_url":   row.get("artwork_url") or "",
        "episode_url":   row.get("episode_url") or "",
        "summary":       row.get("summary_md") or "",
        "source":        row.get("transcript_source"),
        "detail_level":  row.get("detail_level"),
    }}


@app.get("/api/public/episodes/{episode_id}/summary")
def public_summary(episode_id: int, detail_level: str | None = Query(default=None)):
    """No-auth read of a shared summary (a ?ep= link). Only returns already-cached
    summaries — it never generates, and exposes no user data."""
    row = db.get_public_summary(episode_id, detail_level)
    if not row:
        raise HTTPException(status_code=404, detail="Summary not found.")
    return {
        "episode_id":    episode_id,
        "episode_title": row.get("episode_title") or "",
        "podcast_title": row.get("podcast_title") or "",
        "artwork_url":   row.get("artwork_url") or "",
        "episode_url":   row.get("episode_url") or "",
        "summary":       row.get("summary_md") or "",
        "source":        row.get("transcript_source"),
        "detail_level":  row.get("detail_level"),
        "available_levels": db.episode_summary_levels(episode_id),
    }


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
    detail_level: str | None = None   # per-view override; falls back to the profile default


def _resolve_detail(req_level: str | None, profile: dict) -> str:
    """A valid requested level wins (in-panel Quick/Standard/Deep toggle); otherwise
    use the user's saved preference."""
    if req_level and req_level in db.VALID_DETAIL_LEVELS:
        return req_level
    return profile.get("summary_detail") or "standard"


@app.post("/api/summarize")
def summarize_episode(req: SummarizeRequest, user: User = Depends(current_user)):
    profile = db.ensure_profile(user.id, user.email)
    detail_level = _resolve_detail(req.detail_level, profile)

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

    cached = db.get_episode_summary(episode_id, detail_level)
    if cached and cached.get("style_version") == SUMMARY_STYLE_VERSION:
        return {
            "episode_id": episode_id,
            "summary": cached["summary_md"],
            "source": cached.get("transcript_source"),
            "detail_level": detail_level,
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
        detail_level=detail_level,
    )
    summary_md = strip_summary_marker(marked)

    db.save_episode_summary(
        episode_id, summary_md=summary_md, target_words=None,
        transcript_source=source, model=SUMMARY_MODEL, style_version=SUMMARY_STYLE_VERSION,
        detail_level=detail_level,
    )
    db.record_engagement(user.id, episode_id, "summarize")

    return {"episode_id": episode_id, "summary": summary_md, "source": source,
            "detail_level": detail_level, "cached": False}


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


@app.post("/api/subscriptions/{podcast_id}/seed-latest")
def seed_latest_episode(podcast_id: int, user: User = Depends(current_user)):
    """Right after subscribing, summarize the show's most recent episode and drop it
    into the user's My Summaries — so subscribing produces an immediate, visible
    result instead of an empty inbox until the next new episode airs.

    Idempotent and cheap: the summary is globally cached, the delivery is deduped per
    (user, episode), and it bypasses the manual daily cap (system backfill, one ep)."""
    profile = db.ensure_profile(user.id, user.email)
    detail_level = profile.get("summary_detail") or "standard"
    podcast = db.get_podcast(podcast_id)
    if not podcast or not podcast.get("rss_url"):
        raise HTTPException(status_code=404, detail="Podcast not found.")

    try:
        episodes = catalog.episodes_from_rss(podcast["rss_url"], max_results=1)
    except Exception:
        return {"seeded": False, "reason": "episodes_unavailable"}
    if not episodes:
        return {"seeded": False, "reason": "no_episodes"}

    ep = episodes[0]
    episode_id = db.upsert_episode(
        podcast_id, ep["guid"], title=ep["title"], description=ep.get("description", ""),
        audio_url=ep.get("audio_url", ""), episode_url=ep.get("episode_url", ""),
        published_at=ep.get("published_at"), duration_seconds=ep.get("duration_seconds"),
    )

    cached = db.get_episode_summary(episode_id, detail_level)
    if cached and cached.get("style_version") == SUMMARY_STYLE_VERSION:
        summary_md, source = cached["summary_md"], cached.get("transcript_source")
    else:
        text, source = get_transcript(
            podcast["title"], ep["title"], ep.get("description", ""),
            ep.get("audio_url", ""), ep.get("episode_url", ""),
            transcript_url=ep.get("transcript_url", ""), transcript_type=ep.get("transcript_type", ""),
        )
        if not text:
            return {"seeded": False, "reason": "no_transcript"}
        summary_md = strip_summary_marker(summarize(
            podcast["title"], ep["title"], text,
            transcript_source=source, episode_duration=ep.get("duration_seconds"),
            detail_level=detail_level,
        ))
        db.save_episode_summary(
            episode_id, summary_md=summary_md, transcript_source=source,
            model=SUMMARY_MODEL, style_version=SUMMARY_STYLE_VERSION, detail_level=detail_level,
        )

    reasons = [{"type": "show", "podcast_id": podcast_id}]
    did = db.create_delivery(user.id, episode_id, reasons, status="sent")
    if did is not None:
        db.mark_delivery_sent(did)
    return {"seeded": True, "episode_id": episode_id, "episode_title": ep["title"],
            "summary": summary_md, "source": source, "detail_level": detail_level}


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
    person = db.add_tracked_person(user.id, name)
    matches = db.count_person_episode_matches(person["person_id"])
    return {"person": person, "recent_matches": matches}


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
    saved = db.add_tracked_topic(user.id, topic)
    matches = db.count_topic_episode_matches(topic)
    return {"topic": saved, "recent_matches": matches}


@app.delete("/api/topics")
def untrack_topic(topic_id: int = Query(...), user: User = Depends(current_user)):
    db.remove_tracked_topic(user.id, topic_id)
    return {"ok": True}


# ── In-app reader (inbox) ──────────────────────────────────────────────────────

@app.get("/api/deliveries")
def deliveries(user: User = Depends(current_user)):
    return {"deliveries": db.list_deliveries(user.id)}


# ── Recommendations (Discover) ─────────────────────────────────────────────────

@app.get("/api/recommendations")
def recommendations(user: User = Depends(current_user)):
    return {"recommendations": db.recommend_episodes(user.id, limit=20)}


@app.post("/api/episodes/{episode_id}/summarize")
def summarize_existing(
    episode_id: int,
    detail_level: str | None = Query(default=None),
    user: User = Depends(current_user),
):
    """Summarize an episode already in our catalog (e.g. a recommendation),
    using its stored metadata. Respects the global cache + the daily cap."""
    profile = db.ensure_profile(user.id, user.email)
    detail_level = _resolve_detail(detail_level, profile)
    ep = db.get_episode_full(episode_id)
    if not ep:
        raise HTTPException(status_code=404, detail="Episode not found.")

    titles = {"episode_title": ep.get("title") or "", "podcast_title": ep.get("podcast_title") or ""}

    cached = db.get_episode_summary(episode_id, detail_level)
    if cached and cached.get("style_version") == SUMMARY_STYLE_VERSION:
        return {"episode_id": episode_id, "summary": cached["summary_md"],
                "source": cached.get("transcript_source"), "detail_level": detail_level,
                "cached": True, **titles}

    if db.count_user_summaries_today(user.id) >= MANUAL_SUMMARY_DAILY_CAP:
        raise HTTPException(
            status_code=429,
            detail=f"Daily limit of {MANUAL_SUMMARY_DAILY_CAP} on-demand summaries reached.",
        )
    text, source = get_transcript(
        ep["podcast_title"], ep["title"], ep.get("description") or "",
        ep.get("audio_url") or "", ep.get("episode_url") or "",
    )
    if not text:
        raise HTTPException(status_code=422, detail="Could not extract a transcript for this episode.")
    summary_md = strip_summary_marker(summarize(
        ep["podcast_title"], ep["title"], text,
        transcript_source=source, episode_duration=ep.get("duration_seconds"),
        detail_level=detail_level,
    ))
    db.save_episode_summary(episode_id, summary_md=summary_md, transcript_source=source,
                            model=SUMMARY_MODEL, style_version=SUMMARY_STYLE_VERSION,
                            detail_level=detail_level)
    db.record_engagement(user.id, episode_id, "summarize")
    return {"episode_id": episode_id, "summary": summary_md, "source": source,
            "detail_level": detail_level, "cached": False, **titles}


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
                contacts[uid] = {"email": c["email"], "cadence": c["cadence"],
                                 "detail": c.get("detail", "standard")}

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
                contacts[uid] = {"email": s["email"], "cadence": s["cadence"],
                                 "detail": s.get("detail", "standard")}

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

        # Deliveries follow each user's chosen detail level, so generate one summary
        # per distinct level among the matched users (reusing a single transcript fetch).
        levels_needed = {(contacts.get(uid) or {}).get("detail", "standard") for uid in user_reasons}
        summaries = _ensure_summaries(podcast, ep, episode_id, levels_needed, stats, errors)
        if not summaries:
            continue

        for uid, reasons in user_reasons.items():
            did = db.create_delivery(uid, episode_id, reasons, status="queued")
            if did is None:
                continue  # already delivered to this user
            stats["matched"] += 1
            meta = contacts.get(uid) or {}
            level = meta.get("detail", "standard")
            summary_md = summaries.get(level) or summaries.get("standard") or next(iter(summaries.values()))
            if meta.get("cadence") == "instant" and summary_md:
                try:
                    send_summary_email(meta["email"], podcast["title"], ep["title"], summary_md)
                    stats["emails"] += 1
                except Exception as e:
                    errors.append(f"email {meta.get('email')}: {e}")
                db.mark_delivery_sent(did)


def _ensure_summaries(
    podcast: dict, ep: dict, episode_id: int, levels: set, stats: dict, errors: list
) -> dict:
    """Ensure a cached summary exists for each requested detail level, generating any
    missing ones from a single shared transcript fetch. Returns {level: summary_md}."""
    levels = {l for l in levels if l in db.VALID_DETAIL_LEVELS} or {"standard"}
    out: dict[str, str] = {}
    missing: list[str] = []
    for lvl in levels:
        cached = db.get_episode_summary(episode_id, lvl)
        if cached and cached.get("style_version") == SUMMARY_STYLE_VERSION:
            out[lvl] = cached["summary_md"]
        else:
            missing.append(lvl)
    if not missing:
        return out

    text, source = get_transcript(
        podcast["title"], ep["title"], ep.get("description", ""),
        ep.get("audio_url", ""), ep.get("episode_url", ""),
        transcript_url=ep.get("transcript_url", ""), transcript_type=ep.get("transcript_type", ""),
    )
    if not text:
        errors.append(f"no transcript: {podcast.get('title','?')} — {ep.get('title','?')}")
        return out

    for lvl in missing:
        summary_md = strip_summary_marker(summarize(
            podcast["title"], ep["title"], text,
            transcript_source=source, episode_duration=ep.get("duration_seconds"),
            detail_level=lvl,
        ))
        db.save_episode_summary(
            episode_id, summary_md=summary_md, transcript_source=source,
            model=SUMMARY_MODEL, style_version=SUMMARY_STYLE_VERSION, detail_level=lvl,
        )
        stats["summarized"] += 1
        out[lvl] = summary_md
    return out


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
