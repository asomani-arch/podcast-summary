"""Podcast catalog via the unauthenticated Apple iTunes Search API + RSS episode
lists. Used for search and episode browsing so the app needs no extra API keys.

(Podcast Index — lib/podcastindex.py — is reserved for people-scanning in a later
phase, where its episode-level person search is required.)
"""
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from time import mktime

import feedparser
import requests

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"


def _rss_fetch(url: str) -> bytes:
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15, verify=False)
    resp.raise_for_status()
    return resp.content


def search_podcasts(q: str, max_results: int = 20) -> list[dict]:
    r = requests.get(
        ITUNES_SEARCH_URL,
        params={"term": q, "media": "podcast", "entity": "podcast", "limit": max_results},
        headers={"User-Agent": "PodcastAI/5.0"},
        timeout=10,
    )
    r.raise_for_status()
    out: list[dict] = []
    seen: set[str] = set()
    for f in r.json().get("results", []):
        feed_url = f.get("feedUrl", "")
        if not feed_url:
            continue  # no public feed -> not usable
        key = feed_url.lower().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        genre = f.get("primaryGenreName") or ""
        out.append({
            "pi_feed_id":    None,
            "itunes_id":     str(f.get("collectionId") or f.get("trackId") or "") or None,
            "title":         f.get("collectionName") or f.get("trackName") or "",
            "publisher":     f.get("artistName", "") or "",
            "artwork":       f.get("artworkUrl600") or f.get("artworkUrl100") or f.get("artworkUrl60") or "",
            "rss_url":       feed_url,
            "episode_count": f.get("trackCount", 0) or 0,
            "description":   genre,
            "categories":    [genre] if genre else [],
        })
    return out


def _parse_duration_seconds(value) -> int | None:
    if not value:
        return None
    s = str(value).strip()
    if s.isdigit():
        return int(s) or None
    if ":" in s:
        try:
            parts = [int(p) for p in s.split(":")]
        except ValueError:
            return None
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
    return None


def _entry_description(entry: dict) -> str:
    content = entry.get("content") or []
    if content and isinstance(content, list):
        value = content[0].get("value") if isinstance(content[0], dict) else ""
        if value:
            return value
    return entry.get("summary") or entry.get("description") or ""


def _transcript_urls_by_guid(xml_bytes: bytes) -> dict[str, tuple[str, str]]:
    """Parse <podcast:transcript> tags (Podcasting 2.0) out of the raw feed,
    keyed by each item's <guid>. Prefers caption formats (vtt/srt) over html/json
    for clean text. Returns {guid: (url, type)}."""
    out: dict[str, tuple[str, str]] = {}
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return out
    for item in root.iter():
        if item.tag.split("}")[-1] != "item":
            continue
        guid = None
        chosen: tuple[str, str] | None = None
        for ch in item:
            tag = ch.tag.split("}")[-1]
            if tag == "guid" and ch.text:
                guid = ch.text.strip()
            elif tag == "transcript":
                url = ch.attrib.get("url")
                ttype = ch.attrib.get("type", "") or ""
                if not url:
                    continue
                is_caption = "vtt" in ttype.lower() or "srt" in ttype.lower()
                if chosen is None or (is_caption and "html" in (chosen[1] or "").lower()):
                    chosen = (url, ttype)
        if guid and chosen:
            out[guid] = chosen
    return out


def episodes_from_rss(rss_url: str, max_results: int = 50, query: str = "") -> list[dict]:
    raw = _rss_fetch(rss_url)
    feed = feedparser.parse(raw)
    transcripts = _transcript_urls_by_guid(raw)
    # When searching, scan a wider window then trim; otherwise just take the latest.
    window = feed.entries if query else feed.entries[:max_results]
    episodes: list[dict] = []
    for entry in window:
        guid = entry.get("id") or entry.get("link") or entry.get("title", "")
        audio_url = ""
        for enc in entry.get("enclosures", []):
            if "audio" in enc.get("type", ""):
                audio_url = enc.get("href", "")
                break
        published_at = None
        if entry.get("published_parsed"):
            try:
                published_at = datetime.fromtimestamp(
                    mktime(entry.published_parsed), tz=timezone.utc
                ).isoformat()
            except (ValueError, OverflowError, OSError):
                published_at = None
        description = _entry_description(entry)
        transcript_url, transcript_type = transcripts.get(guid, ("", ""))
        episodes.append({
            "pi_episode_id":    None,
            "guid":             guid,
            "title":            entry.get("title", "Untitled"),
            "description":      description,
            "audio_url":        audio_url,
            "episode_url":      entry.get("link", ""),
            "published_at":     published_at,
            "duration_seconds": _parse_duration_seconds(entry.get("itunes_duration", "")),
            "transcript_url":   transcript_url,
            "transcript_type":  transcript_type,
            "persons":          [],
        })

    if query:
        ql = query.lower()
        episodes = [
            e for e in episodes
            if ql in (e["title"] or "").lower()
            or ql in re.sub(r"<[^>]+>", " ", e["description"] or "").lower()
        ][:max_results]
    return episodes
