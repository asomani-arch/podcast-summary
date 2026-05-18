"""Gemini-powered summarization."""
import os
from google import genai

_client = None


def client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


SECTION_TEMPLATES = {
    "overview": (
        "## Overview\n"
        "A 3-4 sentence paragraph capturing what the episode is about, who is featured (host/guests), "
        "and the central themes."
    ),
    "topics": (
        "## Key Topics Discussed\n"
        "A bulleted list of {topic_count} main topics or segments covered, each with a {topic_depth} explanation."
    ),
    "takeaways": (
        "## Key Takeaways\n"
        "A bulleted list of {takeaway_count} actionable insights, lessons, or memorable points."
    ),
    "quotes": (
        "## Notable Quotes or Moments\n"
        "{quote_count} standout quotes, statistics, or moments from the episode "
        "(only include if clearly present in the source material)."
    ),
    "audience": (
        "## Who Should Listen\n"
        "1-2 sentences on the ideal audience for this episode."
    ),
}

# length → (max_output_tokens, depth knobs to fill into section templates)
LENGTH_PROFILES = {
    "short": {
        "tokens":          900,
        "topic_count":     "3-5",
        "topic_depth":     "one sentence",
        "takeaway_count":  "3-5",
        "quote_count":     "1-2",
    },
    "standard": {
        "tokens":          2048,
        "topic_count":     "5-8",
        "topic_depth":     "1-2 sentence",
        "takeaway_count":  "5-7",
        "quote_count":     "2-4",
    },
    "deep": {
        "tokens":          4096,
        "topic_count":     "8-12",
        "topic_depth":     "2-3 sentence",
        "takeaway_count":  "7-10",
        "quote_count":     "3-6",
    },
}

DEFAULT_SECTIONS = ["overview", "topics", "takeaways", "quotes", "audience"]


def _build_system_prompt(length: str, sections: list[str]) -> str:
    profile = LENGTH_PROFILES.get(length, LENGTH_PROFILES["standard"])
    ordered = [s for s in DEFAULT_SECTIONS if s in sections] or DEFAULT_SECTIONS
    rendered = [SECTION_TEMPLATES[s].format(**profile) for s in ordered]
    body = "\n\n".join(rendered)
    return (
        "You are an expert podcast notetaker. Given an episode's title and source material "
        "(full transcript when available, otherwise show notes), produce a detailed, well-structured "
        "summary in markdown.\n\n"
        "Your output MUST follow this exact structure (in this order, using these exact section headers):\n\n"
        f"{body}\n\n"
        "Guidelines:\n"
        "- Write in clear, engaging prose — not robotic.\n"
        "- Be SPECIFIC: use names, numbers, and concrete examples.\n"
        "- Do NOT fabricate. If the source is thin, expand only on what's actually present "
        "and note that detail is limited in the Overview.\n"
        "- Use markdown formatting so the output renders nicely.\n"
    )


def summarize(
    podcast_title: str,
    episode_title: str,
    source_text: str,
    length: str = "standard",
    sections: list[str] | None = None,
) -> str:
    if length not in LENGTH_PROFILES:
        length = "standard"
    sections = sections or DEFAULT_SECTIONS
    system_prompt = _build_system_prompt(length, sections)
    max_tokens = LENGTH_PROFILES[length]["tokens"]

    prompt = (
        f"{system_prompt}\n\n"
        f"Podcast: {podcast_title}\n"
        f"Episode: {episode_title}\n\n"
        f"Source material:\n{source_text[:30000]}"
    )
    response = client().models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={"max_output_tokens": max_tokens, "temperature": 0.4},
    )
    return response.text
