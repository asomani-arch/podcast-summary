"""Podcast Index API client.

Podcast Index (https://podcastindex.org) is the v5 catalog + detection backbone
(docs/PRD.md §5/§7):

  * podcast search                       -> search_podcasts()
  * full episode listings (back catalog) -> episodes_by_feed_id() / _by_feed_url()
  * in-show back-catalog search          -> search_episodes_in_feed()
  * trending shows (the "popular" set)   -> trending_podcasts()
  * episodes featuring a named guest     -> episodes_by_person()   (people-tracking)

Auth (per the Podcast Index docs): every request sends a User-Agent plus three
headers — X-Auth-Key, X-Auth-Date (unix seconds), and Authorization, the latter
being the SHA-1 hex of (key + secret + authDate). Free keys from
api.podcastindex.org. Reads PODCASTINDEX_KEY / PODCASTINDEX_SECRET (falling back
to the v2-era PODCAST_INDEX_* names).
"""
import hashlib
import os
import time
from datetime import datetime, timezone

import requests

BASE_URL = "https://api.podcastindex.org/api/1.0"
USER_AGENT = "PodcastAI/5.0"
_TIMEOUT = 15


class PodcastIndexError(RuntimeError):
    """Raised on missing credentials or a failed Podcast Index request."""


def _credentials() -> tuple[str, str]:
    key = os.getenv("PODCASTINDEX_KEY") or os.getenv("PODCAST_INDEX_KEY")
    secret = os.getenv("PODCASTINDEX_SECRET") or os.getenv("PODCAST_INDEX_SECRET")
    if not key or not secret:
        raise PodcastIndexError("PODCASTINDEX_KEY / PODCASTINDEX_SECRET not set")
    return key, secret


def _auth_headers() -> dict:
    key, secret = _credentials()
    auth_date = str(int(time.time()))
    digest = hashlib.sha1((key + secret + auth_date).encode("utf-8")).hexdigest()
    return {
        "User-Agent": USER_AGENT,
        "X-Auth-Key": key,
        "X-Auth-Date": auth_date,
        "Authorization": digest,
    }


def _get(path: str, params: dict | None = None) -> dict:
    try:
        resp = requests.get(
            f"{BASE_URL}{path}",
            params=params or {},
            headers=_auth_headers(),
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except PodcastIndexError:
        raise
    except Exception as e:  # network / HTTP / JSON
        raise PodcastIndexError(f"{path}: {type(e).__name__}: {e}") from e


# ── Normalizers (Podcast Index shapes -> the shapes the rest of the app uses) ──

def _to_iso(unix_seconds) -> str | None:
    if not unix_seconds:
        return None
    try:
        return datetime.fromtimestamp(int(unix_seconds), tz=timezone.utc).isoformat()
    except (ValueError, OSError, TypeError):
        return None


def _categories(raw) -> list[str]:
    # PI returns categories as a {id: name} object.
    if isinstance(raw, dict):
        return [v for v in raw.values() if v]
    return []


def normalize_podcast(feed: dict) -> dict:
    return {
        "pi_feed_id":    str(feed["id"]) if feed.get("id") else None,
        "itunes_id":     str(feed["itunesId"]) if feed.get("itunesId") else None,
        "title":         feed.get("title", "") or "",
        "publisher":     feed.get("author", "") or feed.get("ownerName", "") or "",
        "artwork":       feed.get("artwork") or feed.get("image", "") or "",
        "rss_url":       feed.get("url", "") or "",
        "episode_count": feed.get("episodeCount", 0) or 0,
        "description":   feed.get("description", "") or "",
        "categories":    _categories(feed.get("categories")),
    }


def normalize_episode(ep: dict) -> dict:
    persons = []
    for p in ep.get("persons") or []:
        name = (p.get("name") or "").strip()
        if name:
            persons.append({
                "name":  name,
                "role":  (p.get("role") or "").strip(),
                "group": (p.get("group") or "").strip(),
            })
    return {
        "pi_episode_id":    str(ep["id"]) if ep.get("id") else None,
        "feed_id":          str(ep["feedId"]) if ep.get("feedId") else None,
        "guid":             ep.get("guid") or ep.get("enclosureUrl") or (str(ep["id"]) if ep.get("id") else ""),
        "title":            ep.get("title", "") or "",
        "description":      ep.get("description", "") or "",
        "audio_url":        ep.get("enclosureUrl", "") or "",
        "episode_url":      ep.get("link", "") or "",
        "published_at":     _to_iso(ep.get("datePublished")),
        "duration_seconds": ep.get("duration") or None,
        "persons":          persons,
    }


# ── Public API ─────────────────────────────────────────────────────────────────

def search_podcasts(term: str, max_results: int = 20) -> list[dict]:
    data = _get("/search/byterm", {"q": term, "max": max_results})
    return [normalize_podcast(f) for f in data.get("feeds", [])]


def podcast_by_feed_id(feed_id: str | int) -> dict | None:
    data = _get("/podcasts/byfeedid", {"id": feed_id})
    feed = data.get("feed")
    # PI returns {} (not a list) for a single feed; guard the empty case.
    return normalize_podcast(feed) if feed else None


def podcast_by_feed_url(url: str) -> dict | None:
    data = _get("/podcasts/byfeedurl", {"url": url})
    feed = data.get("feed")
    return normalize_podcast(feed) if feed else None


def episodes_by_feed_id(feed_id: str | int, max_results: int = 100, since: int | None = None) -> list[dict]:
    params: dict = {"id": feed_id, "max": max_results}
    if since is not None:
        params["since"] = since
    data = _get("/episodes/byfeedid", params)
    return [normalize_episode(e) for e in data.get("items", [])]


def episodes_by_feed_url(url: str, max_results: int = 100) -> list[dict]:
    data = _get("/episodes/byfeedurl", {"url": url, "max": max_results})
    return [normalize_episode(e) for e in data.get("items", [])]


def search_episodes_in_feed(feed_id: str | int, query: str, max_results: int = 200) -> list[dict]:
    """Back-catalog search within one show. Podcast Index has no feed-scoped
    episode search, so fetch the catalog and filter locally on title/description."""
    episodes = episodes_by_feed_id(feed_id, max_results=max_results)
    q = (query or "").strip().lower()
    if not q:
        return episodes
    return [
        e for e in episodes
        if q in (e["title"] or "").lower() or q in (e["description"] or "").lower()
    ]


def episodes_by_person(name: str, max_results: int = 50) -> list[dict]:
    """Episodes featuring a named person — the people-tracking detection path."""
    data = _get("/search/byperson", {"q": name, "max": max_results})
    return [normalize_episode(e) for e in data.get("items", [])]


def trending_podcasts(max_results: int = 100, since: int | None = None, categories: str | None = None) -> list[dict]:
    """Trending shows — used to seed/refresh the curated 'popular' scan set."""
    params: dict = {"max": max_results}
    if since is not None:
        params["since"] = since
    if categories:
        params["cat"] = categories
    data = _get("/podcasts/trending", params)
    return [normalize_podcast(f) for f in data.get("feeds", [])]
