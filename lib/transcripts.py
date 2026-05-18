"""Transcript extraction: YouTube captions -> Gemini audio transcription -> RSS show notes."""
from html import unescape
import os
import re
import tempfile
from urllib.parse import quote, unquote

import requests
from youtube_transcript_api import YouTubeTranscriptApi


def get_transcript(
    podcast_title: str,
    episode_title: str,
    description: str = "",
    audio_url: str = "",
    episode_url: str = "",
) -> tuple[str, str]:
    # 1. YouTube captions.
    yt = _try_youtube(podcast_title, episode_title, description, episode_url)
    if yt:
        return yt, "youtube"

    # 2. Direct audio transcription.
    if audio_url:
        audio_text, partial = _try_audio_gemini(audio_url)
        if audio_text:
            return audio_text, "audio_partial" if partial else "audio"

    # 3. RSS show notes fallback.
    clean = re.sub(r"<[^>]+>", "", description or "").strip()
    return clean, "shownotes"


def _try_youtube(
    podcast_title: str,
    episode_title: str,
    description: str = "",
    episode_url: str = "",
) -> str | None:
    video_ids = _extract_youtube_video_ids(description)
    video_ids.extend(_extract_youtube_video_ids(episode_url))

    for page_url in _candidate_page_urls(episode_url):
        page_text = _fetch_page_text(page_url)
        if page_text:
            video_ids.extend(_extract_youtube_video_ids(page_text))

    searched = _search_youtube(f"{podcast_title} {episode_title}")
    if searched:
        video_ids.append(searched)

    seen: set[str] = set()
    for video_id in video_ids:
        if video_id in seen:
            continue
        seen.add(video_id)
        transcript = _fetch_youtube_transcript(video_id)
        if transcript:
            return transcript
    return None


def _extract_youtube_video_ids(text: str) -> list[str]:
    if not text:
        return []

    decoded = unquote(unescape(text))
    patterns = (
        r"(?:youtube\.com/watch\?[^\"'<>\s]*v=|youtu\.be/)([a-zA-Z0-9_-]{11})",
        r"youtube\.com/(?:embed|shorts)/([a-zA-Z0-9_-]{11})",
        r'"videoId":"([a-zA-Z0-9_-]{11})"',
    )

    ids: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, decoded):
            video_id = match.group(1)
            if video_id not in seen:
                seen.add(video_id)
                ids.append(video_id)
    return ids


def _fetch_page_text(url: str) -> str:
    if not url or not re.match(r"^https?://", url):
        return ""
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        resp.raise_for_status()
        return resp.text
    except Exception:
        return ""


def _candidate_page_urls(url: str) -> list[str]:
    if not url:
        return []

    candidates = [url]
    match = re.match(r"^(https?://(?:www\.)?colossus\.com/episode/)([^/]+)/?$", url)
    if match:
        base, slug = match.groups()
        if not slug.startswith("the-"):
            candidates.append(f"{base}the-{slug}/")
    return candidates


def _fetch_youtube_transcript(video_id: str) -> str:
    try:
        if hasattr(YouTubeTranscriptApi, "get_transcript"):
            segments = YouTubeTranscriptApi.get_transcript(video_id)
        else:
            segments = YouTubeTranscriptApi().fetch(video_id)

        texts = []
        for segment in segments:
            if isinstance(segment, dict):
                text = segment.get("text", "")
            else:
                text = getattr(segment, "text", "")
            if text:
                texts.append(text)
        return " ".join(texts)
    except Exception:
        return ""


def _search_youtube(query: str) -> str | None:
    try:
        url = f"https://www.youtube.com/results?search_query={quote(query)}"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        match = re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', resp.text)
        return match.group(1) if match else None
    except Exception:
        return None


def _try_audio_gemini(audio_url: str) -> tuple[str, bool]:
    """Download audio (up to 50 MB by default) and transcribe via Gemini."""
    try:
        max_bytes = int(os.getenv("MAX_AUDIO_TRANSCRIPTION_BYTES", str(50 * 1024 * 1024)))
    except ValueError:
        max_bytes = 50 * 1024 * 1024
    try:
        from google import genai

        resp = requests.get(audio_url, timeout=30, stream=True, verify=False)
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        if "m4a" in content_type or audio_url.lower().endswith(".m4a"):
            suffix = ".m4a"
        elif "ogg" in content_type:
            suffix = ".ogg"
        elif "wav" in content_type:
            suffix = ".wav"
        else:
            suffix = ".mp3"

        tmp_path = None
        try:
            partial = False
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
                tmp_path = f.name
                downloaded = 0
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    remaining = max_bytes - downloaded
                    if remaining <= 0:
                        partial = True
                        break
                    if len(chunk) > remaining:
                        f.write(chunk[:remaining])
                        downloaded += remaining
                        partial = True
                        break
                    f.write(chunk)
                    downloaded += len(chunk)

            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
            uploaded = client.files.upload(path=tmp_path)
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=["Please transcribe the provided podcast audio accurately:", uploaded],
            )
            return response.text or "", partial
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    except Exception:
        return "", False
