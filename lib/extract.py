"""Cheap guest + topic extraction from episode metadata (title + description).

Run during the scan pass to detect tracked people/topics BEFORE doing expensive
transcription/summarization — only episodes that match someone get summarized
(docs/PRD.md §7.3 cost control). Uses Gemini with JSON output and thinking off.
"""
import json
import os
import re

_client = None


def client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


def extract_people_and_topics(
    podcast_title: str,
    episode_title: str,
    description: str = "",
    pi_persons: list[dict] | None = None,
) -> dict:
    """Returns {"people": [Full Name, ...], "topics": [short phrase, ...]}.
    Seeds people from any podcast:person RSS tags (high precision), then augments
    with an LLM pass over the metadata."""
    people: list[str] = []
    seen: set[str] = set()
    for p in (pi_persons or []):
        n = (p.get("name") or "").strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            people.append(n)

    clean_desc = re.sub(r"<[^>]+>", " ", description or "")[:4000]
    prompt = (
        "From this podcast episode's metadata, extract two things:\n"
        "1. \"people\": guests or notable named individuals featured/interviewed "
        "(real human full names only; skip the regular host unless they're the "
        "named subject). \n"
        "2. \"topics\": the main themes as short phrases (e.g. 'AI infrastructure', "
        "'private credit', 'leadership').\n"
        "Return STRICT JSON: {\"people\": [\"Full Name\"], \"topics\": [\"topic\"]}. "
        "Use empty arrays if none. Never invent names or topics unsupported by the text.\n\n"
        f"Podcast: {podcast_title}\nEpisode: {episode_title}\nDescription: {clean_desc}"
    )
    try:
        resp = client().models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={
                "max_output_tokens": 600,
                "temperature": 0.1,
                "thinking_config": {"thinking_budget": 0},
                "response_mime_type": "application/json",
            },
        )
        data = json.loads(resp.text or "{}")
    except Exception as e:
        print(f"extract failed: {type(e).__name__}: {e}", flush=True)
        return {"people": people, "topics": []}

    for n in data.get("people", []) or []:
        n = (n or "").strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            people.append(n)
    topics = []
    tseen: set[str] = set()
    for t in data.get("topics", []) or []:
        t = (t or "").strip()
        if t and t.lower() not in tseen:
            tseen.add(t.lower())
            topics.append(t)
    return {"people": people, "topics": topics[:8]}
