"""Get the best available text source for an episode.

Strategy (hybrid):
  1. Try to find a YouTube version of the episode and fetch its transcript.
  2. Fall back to the RSS show notes / description.

Returns: (text, source) where source is 'youtube' or 'shownotes'.
"""
import re
import requests
from urllib.parse import quote
from youtube_transcript_api import YouTubeTranscriptApi


def get_transcript(podcast_title: str, episode_title: str, description: str) -> tuple[str, str]:
    # 1. Attempt YouTube
    yt_text = _try_youtube(podcast_title, episode_title)
    if yt_text:
        return yt_text, "youtube"

    # 2. Fall back to show notes (strip HTML)
    clean = re.sub(r"<[^>]+>", "", description or "").strip()
    return clean, "shownotes"


def _try_youtube(podcast_title: str, episode_title: str) -> str | None:
    """Search YouTube for the episode and return its transcript if found."""
    try:
        video_id = _search_youtube(f"{podcast_title} {episode_title}")
        if not video_id:
            return None
        segments = YouTubeTranscriptApi.get_transcript(video_id)
        return " ".join(seg["text"] for seg in segments)
    except Exception:
        return None


def _search_youtube(query: str) -> str | None:
    """Naive scrape of YouTube search results to grab the first video ID.
    For production, swap in the YouTube Data API for reliability.
    """
    try:
        url = f"https://www.youtube.com/results?search_query={quote(query)}"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        match = re.search(r"\"videoId\":\"([a-zA-Z0-9_-]{11})\"", resp.text)
        return match.group(1) if match else None
    except Exception:
        return None
