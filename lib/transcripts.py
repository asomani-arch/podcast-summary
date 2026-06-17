"""Transcript extraction: YouTube captions -> Gemini audio transcription -> RSS show notes."""
from html import unescape
import json
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
    transcript_url: str = "",
    transcript_type: str = "",
) -> tuple[str, str]:
    # 1. Official logged-in Colossus transcript, when a session cookie is configured.
    colossus = _try_colossus_transcript(episode_url)
    if colossus:
        return colossus, "colossus"

    # 2. Publisher-provided transcript from the RSS feed (Podcasting 2.0). Free,
    #    accurate, instant — the best source when the show publishes one.
    published = _try_published_transcript(transcript_url, transcript_type)
    if published:
        return published, "published"

    # 3. Deepgram — full, accurate transcription straight from the audio URL.
    #    Reliable from datacenter IPs (unlike YouTube captions) and not size-capped,
    #    so this is the primary path for long episodes when a key is configured.
    deepgram = _try_deepgram(audio_url)
    if deepgram:
        return deepgram, "deepgram"

    # 4. YouTube captions (free, but often IP-blocked from servers without a proxy).
    yt = _try_youtube(podcast_title, episode_title, description, episode_url)
    if yt:
        return yt, "youtube"

    # 5. Direct audio transcription via Gemini (size-capped fallback).
    if audio_url:
        audio_text, partial = _try_audio_gemini(audio_url)
        if audio_text:
            return audio_text, "audio_partial" if partial else "audio"

    # 6. RSS show notes fallback.
    clean = re.sub(r"<[^>]+>", "", description or "").strip()
    return clean, "shownotes"


def _cues_to_text(body: str) -> str:
    """Flatten a WebVTT or SRT caption file into plain prose."""
    lines: list[str] = []
    for raw in body.splitlines():
        s = raw.strip()
        if not s or s == "WEBVTT" or s.startswith(("NOTE", "STYLE")):
            continue
        if "-->" in s or s.isdigit():
            continue
        s = re.sub(r"<[^>]+>", "", s)  # strip inline cue tags like <c> or <00:00:01.000>
        if s:
            lines.append(s)
    # Auto-captions often repeat a line across rolling cues; drop consecutive dupes.
    deduped: list[str] = []
    for s in lines:
        if not deduped or deduped[-1] != s:
            deduped.append(s)
    return " ".join(deduped).strip()


def _try_published_transcript(url: str, ttype: str = "") -> str | None:
    """Fetch a publisher-provided transcript URL (Podcasting 2.0) and convert it
    to plain text. Handles VTT/SRT, JSON (Whisper-style), HTML, and plain text."""
    if not url:
        return None
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20, verify=False)
        resp.raise_for_status()
        body = resp.text
    except Exception as e:
        print(f"published transcript fetch failed: {type(e).__name__}: {e}", flush=True)
        return None

    t = (ttype or "").lower()
    u = url.lower()
    try:
        if "json" in t or u.endswith(".json"):
            data = json.loads(body)
            if isinstance(data, dict) and isinstance(data.get("text"), str):
                return data["text"].strip() or None
            segs = data.get("segments", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            text = " ".join((s.get("body") or s.get("text") or "") for s in segs if isinstance(s, dict))
            return text.strip() or None
        if "vtt" in t or "srt" in t or u.endswith((".vtt", ".srt")):
            return _cues_to_text(body) or None
        if "html" in t or u.endswith((".html", ".htm")) or "<" in body[:200]:
            return unescape(re.sub(r"<[^>]+>", " ", body)).strip() or None
        return body.strip() or None
    except Exception as e:
        print(f"published transcript parse failed: {type(e).__name__}: {e}", flush=True)
        return None


def _try_deepgram(audio_url: str) -> str | None:
    """Transcribe a remote audio URL with Deepgram's prerecorded API. Deepgram
    fetches the audio itself (no large download into our function) and returns a
    full transcript, handling multi-hour episodes well."""
    key = os.getenv("DEEPGRAM_API_KEY")
    if not key or not audio_url:
        return None
    try:
        resp = requests.post(
            "https://api.deepgram.com/v1/listen",
            params={
                "model": "nova-3",
                "smart_format": "true",
                "punctuate": "true",
                "paragraphs": "true",
            },
            headers={"Authorization": f"Token {key}", "Content-Type": "application/json"},
            json={"url": audio_url},
            timeout=290,
        )
        resp.raise_for_status()
        alt = resp.json()["results"]["channels"][0]["alternatives"][0]
        text = (alt.get("paragraphs", {}) or {}).get("transcript") or alt.get("transcript", "")
        return text or None
    except Exception as e:
        print(f"deepgram failed: {type(e).__name__}: {e}", flush=True)
        return None


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
        headers = {"User-Agent": "Mozilla/5.0"}
        cookie = os.getenv("COLOSSUS_COOKIE")
        if cookie and "colossus.com" in url:
            headers["Cookie"] = cookie
        resp = requests.get(url, headers=headers, timeout=10)
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


def _try_colossus_transcript(episode_url: str) -> str | None:
    if not os.getenv("COLOSSUS_COOKIE"):
        return None

    for page_url in _candidate_page_urls(episode_url):
        page_text = _fetch_page_text(page_url)
        transcript = _extract_colossus_transcript(page_text)
        if transcript:
            return transcript
    return None


def _extract_colossus_transcript(page_text: str) -> str:
    if not page_text:
        return ""

    start = page_text.find('<div class="transcript__content">')
    end = page_text.find("</article>", start)
    if start == -1 or end == -1:
        return ""

    html = page_text[start:end]
    html = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
    html = re.sub(r"<style\b[^>]*>.*?</style>", " ", html, flags=re.I | re.S)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"</p\s*>", "\n", html, flags=re.I)
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text).strip()

    # Public Colossus pages expose only the intro plus a gate. Treat that as
    # unavailable so the resolver continues to YouTube/audio.
    if "content-gate-obscure" in html and len(text) < 5000:
        return ""
    return text if len(text) >= 2000 else ""


def _fetch_youtube_transcript(video_id: str) -> str:
    try:
        if hasattr(YouTubeTranscriptApi, "get_transcript"):
            segments = YouTubeTranscriptApi.get_transcript(video_id)
        else:
            segments = _youtube_transcript_api().fetch(video_id)

        texts = []
        for segment in segments:
            if isinstance(segment, dict):
                text = segment.get("text", "")
            else:
                text = getattr(segment, "text", "")
            if text:
                texts.append(text)
        return " ".join(texts)
    except Exception as e:
        print(f"youtube transcript failed for {video_id}: {type(e).__name__}: {e}", flush=True)
        return ""


def _youtube_transcript_api() -> YouTubeTranscriptApi:
    proxy_url = os.getenv("YOUTUBE_TRANSCRIPT_PROXY_URL")
    webshare_user = os.getenv("WEBSHARE_PROXY_USERNAME")
    webshare_pass = os.getenv("WEBSHARE_PROXY_PASSWORD")

    if proxy_url:
        from youtube_transcript_api.proxies import GenericProxyConfig

        return YouTubeTranscriptApi(
            proxy_config=GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)
        )
    if webshare_user and webshare_pass:
        from youtube_transcript_api.proxies import WebshareProxyConfig

        return YouTubeTranscriptApi(
            proxy_config=WebshareProxyConfig(
                proxy_username=webshare_user,
                proxy_password=webshare_pass,
            )
        )
    return YouTubeTranscriptApi()


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
            uploaded = client.files.upload(file=tmp_path)
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=["Please transcribe the provided podcast audio accurately:", uploaded],
            )
            if not response.text:
                print("gemini audio transcription returned empty text", flush=True)
            return response.text or "", partial
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    except Exception as e:
        print(f"gemini audio transcription failed: {type(e).__name__}: {e}", flush=True)
        return "", False
