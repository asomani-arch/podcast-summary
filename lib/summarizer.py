"""Gemini-powered summarization."""
import os
import re

SUMMARY_STYLE_VERSION = "pe-newsletter-v3"
SUMMARY_MARKER = f"<!-- summary_style:{SUMMARY_STYLE_VERSION} -->"

_client = None


def client():
    global _client
    if _client is None:
        from google import genai

        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


# Length remains a feed-level preference, but the final brief is also scaled by
# episode duration when the RSS feed provides it.
LENGTH_PROFILES = {
    "short": {"fallback_words": (200, 260), "tokens": 1000},
    "standard": {"fallback_words": (300, 380), "tokens": 1500},
    "deep": {"fallback_words": (425, 500), "tokens": 2200},
}

DEFAULT_SECTIONS = ["overview", "takeaways"]
FULL_TRANSCRIPT_SOURCES = {"youtube", "audio"}
SOURCE_LABELS = {
    "audio_partial": "a partial audio transcript",
    "shownotes": "RSS show notes",
}


def summary_is_current(summary: str | None) -> bool:
    return bool(summary and summary.lstrip().startswith(SUMMARY_MARKER))


def strip_summary_marker(summary: str | None) -> str:
    if not summary:
        return ""
    return summary.replace(SUMMARY_MARKER, "", 1).lstrip()


def _mark_summary(summary: str) -> str:
    cleaned = strip_summary_marker(summary)
    return f"{SUMMARY_MARKER}\n{cleaned}"


def _parse_duration_seconds(duration: str | int | None) -> int | None:
    if duration is None:
        return None
    if isinstance(duration, int):
        return duration if duration > 0 else None

    value = str(duration).strip().lower()
    if not value:
        return None
    if value.isdigit():
        seconds = int(value)
        return seconds if seconds > 0 else None

    if ":" in value:
        try:
            parts = [int(p) for p in value.split(":")]
        except ValueError:
            parts = []
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]

    hours = re.search(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hour)", value)
    minutes = re.search(r"(\d+(?:\.\d+)?)\s*(?:m|min|minute)", value)
    seconds = 0
    if hours:
        seconds += int(float(hours.group(1)) * 3600)
    if minutes:
        seconds += int(float(minutes.group(1)) * 60)
    return seconds or None


def _word_range(length: str, duration: str | int | None) -> tuple[int, int]:
    seconds = _parse_duration_seconds(duration)
    if seconds:
        minutes = seconds / 60
        if minutes < 30:
            return 200, 260
        if minutes < 60:
            return 275, 350
        if minutes < 90:
            return 350, 425
        return 425, 500
    return LENGTH_PROFILES.get(length, LENGTH_PROFILES["standard"])["fallback_words"]


def _build_system_prompt(
    length: str,
    transcript_source: str,
    episode_duration: str | int | None,
) -> tuple[str, int]:
    profile = LENGTH_PROFILES.get(length, LENGTH_PROFILES["standard"])
    min_words, max_words = _word_range(length, episode_duration)
    full_transcript = transcript_source in FULL_TRANSCRIPT_SOURCES
    source_note_requirement = ""
    if not full_transcript:
        source_label = SOURCE_LABELS.get(transcript_source, "the available source material")
        source_note_requirement = (
            "- Start with this exact italicized line before the first heading:\n"
            f"  *Source note: Full transcript unavailable; this brief is based on {source_label}.*\n"
        )

    prompt = (
        "You write smart newsletter-style podcast briefs for private equity investment "
        "professionals. Use the transcript as the source of truth. If the source is only "
        "show notes, be explicit about that limitation and do not infer beyond the evidence.\n\n"
        "Output requirements:\n"
        f"- Write {min_words}-{max_words} words total.\n"
        "- Use markdown.\n"
        f"{source_note_requirement}"
        "- Use exactly these sections, in this order:\n"
        "  ## Overview\n"
        "  One polished paragraph that explains the episode's core argument, why it matters, "
        "and the private-equity context.\n"
        "  ## Key Takeaways\n"
        "  4-7 bullets, each with a bold takeaway label followed by concise analysis.\n\n"
        "Private-equity lens:\n"
        "- Prioritize insights relevant to deal sourcing, diligence, underwriting, portfolio "
        "operations, value creation, management quality, industry structure, competitive "
        "advantage, growth durability, unit economics, capital allocation, regulatory risk, "
        "and exit implications when they are supported by the source.\n"
        "- Mention companies, sectors, markets, and numbers only when present in the source.\n"
        "- Translate generic discussion into investor-relevant implications without fabricating "
        "facts or pretending the episode said more than it did.\n"
        "- Avoid filler, hype, generic podcast recap language, and standalone quote sections.\n"
    )
    return prompt, profile["tokens"]


def summarize(
    podcast_title: str,
    episode_title: str,
    source_text: str,
    length: str = "standard",
    sections: list[str] | None = None,
    transcript_source: str = "",
    episode_duration: str | int | None = None,
) -> str:
    if length not in LENGTH_PROFILES:
        length = "standard"
    system_prompt, max_tokens = _build_system_prompt(
        length,
        transcript_source,
        episode_duration,
    )

    prompt = (
        f"{system_prompt}\n\n"
        f"Podcast: {podcast_title}\n"
        f"Episode: {episode_title}\n"
        f"Transcript/source type: {transcript_source or 'unknown'}\n\n"
        f"Source material:\n{source_text[:30000]}"
    )
    response = client().models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={"max_output_tokens": max_tokens, "temperature": 0.35},
    )
    return _mark_summary(response.text or "")
