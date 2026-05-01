"""Gemini-powered summarization."""
import os
from google import genai

_client = None


def client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


SYSTEM_PROMPT = """You are an expert podcast notetaker. Given an episode's title and source material (full transcript when available, otherwise show notes), produce a detailed, well-structured summary in markdown.

Your output MUST follow this exact structure:

## Overview
A 3-4 sentence paragraph capturing what the episode is about, who is featured (host/guests), and the central themes.

## Key Topics Discussed
A bulleted list of 5-8 main topics or segments covered, each with a 1-2 sentence explanation.

## Key Takeaways
A bulleted list of 5-7 actionable insights, lessons, or memorable points.

## Notable Quotes or Moments
2-4 standout quotes, statistics, or moments from the episode (only include if clearly present in the source material).

## Who Should Listen
1-2 sentences on the ideal audience for this episode.

Guidelines:
- Write in clear, engaging prose — not robotic.
- Be SPECIFIC: use names, numbers, and concrete examples.
- Do NOT fabricate. If the source is thin, expand only on what's actually present and note that detail is limited in the Overview.
- Use markdown formatting so the output renders nicely.
"""


def summarize(podcast_title: str, episode_title: str, source_text: str) -> str:
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Podcast: {podcast_title}\n"
        f"Episode: {episode_title}\n\n"
        f"Source material:\n{source_text[:30000]}"
    )
    response = client().models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={"max_output_tokens": 2048, "temperature": 0.4},
    )
    return response.text
