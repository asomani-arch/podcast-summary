"""Gemini-powered summarization.

v5 produces a *layered* investor-grade brief: a 10-second read at the top
(TL;DR + bottom-line takeaways) followed by an exhaustive section-by-section
walkthrough sized to the episode's length. The lens is fixed private-equity for
every reader, which is what lets a single summary be cached and reused across
users (see docs/PRD.md §7).
"""
import os
import re

SUMMARY_STYLE_VERSION = "pe-layered-v6"  # v6: exclude sponsor/ad reads from the brief
SUMMARY_MARKER = f"<!-- summary_style:{SUMMARY_STYLE_VERSION} -->"

_client = None


def client():
    global _client
    if _client is None:
        from google import genai

        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


# Length is driven by episode duration: ~1,000 words per hour of audio, with a
# floor (short episodes still get a real brief) and a soft cap (a 4-hour epic
# stays readable). See docs/PRD.md §6.
WORDS_PER_HOUR = 1000
MIN_TARGET_WORDS = 400
MAX_TARGET_WORDS = 3000
DEFAULT_DURATION_SECONDS = 3600  # assume ~1 hour when the feed omits duration

# Sources that give us the full spoken content; anything else is partial and the
# brief must say so and stay within what the material supports.
FULL_TRANSCRIPT_SOURCES = {"published", "colossus", "deepgram", "youtube", "audio"}
SOURCE_NOTES = {
    "audio_partial": (
        "Full transcript unavailable; this brief is based on a partial audio "
        "transcript and the later portion of the episode may not be reflected."
    ),
    "shownotes": (
        "Full transcript unavailable; this brief is based only on the RSS show "
        "notes, so it is necessarily limited and does not infer beyond them."
    ),
}

# Gemini 2.5 Flash has a 1M-token context window, so we can pass the whole
# transcript of even a 3-hour episode. The old 30k-char cap truncated long
# transcripts to ~5k words, which defeated the point of scaling length up.
MAX_SOURCE_CHARS = int(os.getenv("MAX_SUMMARY_SOURCE_CHARS", str(500_000)))


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


# Detail levels titrate length + depth on top of the duration baseline.
# 'standard' reproduces the original behavior exactly (mult 1.0, same floor/cap).
DETAIL_LEVELS = {
    "quick":    {"mult": 0.35, "min": 120, "max": 350},
    "standard": {"mult": 1.0,  "min": MIN_TARGET_WORDS, "max": MAX_TARGET_WORDS},
    "deep":     {"mult": 1.8,  "min": 600, "max": 5000},
}
DEFAULT_DETAIL_LEVEL = "standard"


def _target_words(duration_seconds: int | None, detail_level: str = DEFAULT_DETAIL_LEVEL) -> int:
    seconds = duration_seconds or DEFAULT_DURATION_SECONDS
    raw = WORDS_PER_HOUR * seconds / 3600
    cfg = DETAIL_LEVELS.get(detail_level, DETAIL_LEVELS[DEFAULT_DETAIL_LEVEL])
    return max(cfg["min"], min(cfg["max"], round(raw * cfg["mult"])))


def _duration_phrase(duration_seconds: int | None) -> str:
    if not duration_seconds:
        return ""
    hours, rem = divmod(duration_seconds, 3600)
    minutes = rem // 60
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return " ".join(parts)


def _build_prompt(
    podcast_title: str,
    episode_title: str,
    source_text: str,
    transcript_source: str,
    duration_seconds: int | None,
    focus_person: str | None,
    detail_level: str = DEFAULT_DETAIL_LEVEL,
) -> tuple[str, int]:
    target_words = _target_words(duration_seconds, detail_level)

    # Never ask for more output than the source can actually support — this stops
    # a thin show-notes blurb being padded into a fabricated 1,000-word essay.
    source_words = len(source_text.split())
    if source_words < 2 * target_words:
        target_words = max(150, min(target_words, source_words // 2 or 150))

    full_transcript = transcript_source in FULL_TRANSCRIPT_SOURCES
    source_note_requirement = ""
    if not full_transcript:
        note = SOURCE_NOTES.get(transcript_source, "Full transcript unavailable; this brief is based on limited source material.")
        source_note_requirement = (
            "Begin with this exact italicized line, before the first heading:\n"
            f"  *Source note: {note}*\n\n"
        )

    duration_phrase = _duration_phrase(duration_seconds)
    length_note = (
        f"The episode runs about {duration_phrase}. " if duration_phrase else ""
    )

    focus_instruction = ""
    if focus_person:
        focus_instruction = (
            f" This brief is being delivered to a reader who follows **{focus_person}**: "
            f"while still summarizing the whole episode, give particular weight to "
            f"{focus_person}'s contributions, claims, and the segments featuring them."
        )

    if detail_level == "quick":
        sections_block = (
            f"{length_note}Write a fast-skim brief of about {target_words} words. Use "
            "GitHub-flavored markdown. Produce exactly these two sections, and nothing else:\n\n"
            "## TL;DR\n"
            "2-3 sentences capturing the single most investor-relevant thread of the episode.\n\n"
            "## Bottom-Line Takeaways\n"
            "3-5 bullets. Start each with a **bold takeaway** then a sentence of crisp "
            "analysis. These are the punchlines an investor would repeat."
            f"{focus_instruction}\n\n"
            "Do NOT add a detailed walkthrough, quotes, company lists, or any other "
            "section — keep it to the two headings above.\n\n"
        )
    else:
        depth_note = ""
        if detail_level == "deep":
            depth_note = (
                "This is a DEEP brief for a reader who wants maximum coverage: be "
                "exhaustive in the walkthrough, use more ### subheadings, and capture more "
                "of the specific quotes, figures, and back-and-forth than you otherwise would.\n\n"
            )
        sections_block = (
            f"{length_note}Write approximately {target_words} words total. Use "
            "GitHub-flavored markdown. Produce exactly these sections, in this order:\n\n"
            f"{depth_note}"
            "## TL;DR\n"
            "2-3 sentences capturing the single most investor-relevant thread of the episode.\n\n"
            "## Bottom-Line Takeaways\n"
            "3-5 bullets. Start each with a **bold takeaway** then a sentence or two of "
            "crisp analysis. These are the punchlines an investor would repeat.\n\n"
            "## Detailed Walkthrough\n"
            "The substance, and where most of the words go. Use ### thematic subheadings "
            "(one per major thread or segment) and cover every meaningful argument, "
            "example, data point, and disagreement in the episode."
            f"{focus_instruction}\n\n"
            "## Notable Quotes\n"
            "Only if there are genuinely striking, verbatim quotes: 1-3 short quotes with "
            "attribution. Omit this heading entirely if nothing stands out.\n\n"
            "## Companies, Sectors & Numbers\n"
            "Only when present in the source: a compact list of the specific companies, "
            "sectors, markets, and figures mentioned, for an investor scanning for names. "
            "Omit this heading entirely if the episode is abstract.\n\n"
        )

    prompt = (
        "You write detailed, investor-grade podcast briefs for time-constrained "
        "private-equity professionals. Your reader wants to *not miss anything that "
        "matters* but triages in seconds, so the brief is layered: the top is a "
        "10-second read, the body rewards a deeper read.\n\n"
        "Use the transcript as the single source of truth. Never fabricate or imply "
        "the episode said more than it did.\n\n"
        "Ignore advertising entirely. Podcast transcripts interleave sponsor reads, "
        "ad spots, and promotional segments with the actual conversation — these are "
        "NOT part of the episode's content. Do not summarize them, and never let "
        "advertiser names, promo codes, discount URLs, or sponsor figures appear in "
        "the brief (especially not in Companies, Sectors & Numbers). A company belongs "
        "in the brief only if it is genuinely discussed in the conversation, not merely "
        "advertised. Telltale signs of an ad to skip: 'this episode is brought to you "
        "by', 'use code', 'visit <site>.com/<show>', 'sign up today', recurring "
        "host-read pitches for unrelated products.\n\n"
        f"{source_note_requirement}"
        f"{sections_block}"
        "Private-equity lens:\n"
        "- Prioritize insight relevant to deal sourcing, diligence, underwriting, "
        "portfolio operations, value creation, management quality, industry structure, "
        "competitive advantage, growth durability, unit economics, capital allocation, "
        "regulatory risk, and exit implications — wherever the source supports it.\n"
        "- Mention companies, sectors, markets, and numbers only when present in the source.\n"
        "- Translate generic discussion into investor-relevant implications without "
        "inventing facts.\n"
        "- No hype, no filler, no generic 'in this episode' recap language.\n\n"
        f"Podcast: {podcast_title}\n"
        f"Episode: {episode_title}\n"
        f"Transcript/source type: {transcript_source or 'unknown'}\n\n"
        f"Source material:\n{source_text[:MAX_SOURCE_CHARS]}"
    )

    # Allow generous room for the target word count plus markdown structure.
    max_tokens = int(target_words * 2.5) + 1200
    return prompt, max_tokens


def summarize(
    podcast_title: str,
    episode_title: str,
    source_text: str,
    length: str = "standard",          # accepted for backward compat; length is
    sections: list[str] | None = None,  # now duration-driven and the format is fixed
    transcript_source: str = "",
    episode_duration: str | int | None = None,
    focus_person: str | None = None,
    detail_level: str = DEFAULT_DETAIL_LEVEL,
) -> str:
    duration_seconds = _parse_duration_seconds(episode_duration)
    prompt, max_tokens = _build_prompt(
        podcast_title,
        episode_title,
        source_text,
        transcript_source,
        duration_seconds,
        focus_person,
        detail_level,
    )
    response = client().models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={
            "max_output_tokens": max_tokens,
            "temperature": 0.35,
            # Gemini 2.5 Flash "thinks" by default, and those hidden tokens eat the
            # output budget — which truncated summaries to a stub. Turn it off so the
            # full budget goes to the actual brief.
            "thinking_config": {"thinking_budget": 0},
        },
    )
    return _mark_summary(response.text or "")
