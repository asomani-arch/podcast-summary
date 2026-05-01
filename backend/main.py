import os
import re
import feedparser
import requests
from google import genai
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

app = FastAPI(title="Podcast Summary App")

# Serve frontend
app.mount("/static", StaticFiles(directory="../frontend/static"), name="static")

SYSTEM_PROMPT = """You are an expert podcast notetaker. Given an episode's title and description/show notes, produce a detailed, well-structured summary in markdown format.

Your output MUST follow this exact structure:

## Overview
A 3-4 sentence paragraph capturing what the episode is about, who is featured (host/guests), and the central themes.

## Key Topics Discussed
A bulleted list of 5-8 main topics or segments covered, each with a one-sentence explanation.

## Key Takeaways
A bulleted list of 5-7 actionable insights, lessons, or memorable points.

## Notable Quotes or Moments
2-4 standout quotes, statistics, or moments from the episode (only include if clearly present in the source material).

## Who Should Listen
1-2 sentences on the ideal audience for this episode.

Guidelines:
- Write in clear, engaging prose — not robotic.
- Be SPECIFIC: use names, numbers, and concrete examples from the source material.
- Do NOT fabricate content. If the source description is short, expand only on what's actually mentioned. Note in the Overview if details are limited.
- Use markdown formatting (headers, bold, bullets) so the output renders nicely.
"""


class PodcastRequest(BaseModel):
    rss_url: str
    num_episodes: int = 5


@app.get("/")
def serve_frontend():
    return FileResponse("../frontend/index.html")


@app.post("/api/summarize")
def summarize_podcast(request: PodcastRequest):
    # Fetch and parse RSS feed (using requests to handle SSL on Windows)
    try:
        headers = {"User-Agent": "Mozilla/5.0 (podcast-summary-app)"}
        resp = requests.get(request.rss_url, headers=headers, timeout=10, verify=False)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except requests.RequestException as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch RSS feed: {str(e)}")

    if not feed.entries:
        raise HTTPException(status_code=400, detail="No episodes found. Please check the RSS URL.")

    podcast_title = feed.feed.get("title", "Unknown Podcast")
    episodes = feed.entries[: request.num_episodes]

    summaries = []
    for episode in episodes:
        title = episode.get("title", "Untitled Episode")
        description = episode.get("summary", episode.get("description", ""))
        published = episode.get("published", "Unknown date")

        # Strip HTML tags from description
        description_clean = re.sub(r"<[^>]+>", "", description).strip()

        if not description_clean:
            summaries.append({
                "title": title,
                "published": published,
                "summary": "No description available for this episode.",
            })
            continue

        try:
            prompt = (
                f"{SYSTEM_PROMPT}\n\n"
                f"Podcast: {podcast_title}\n"
                f"Episode: {title}\n\n"
                f"Description:\n{description_clean[:3000]}"
            )
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config={"max_output_tokens": 2048, "temperature": 0.4},
            )
            summary_text = response.text
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        summaries.append({
            "title": title,
            "published": published,
            "summary": summary_text,
        })

    return {
        "podcast_title": podcast_title,
        "episodes": summaries,
    }
