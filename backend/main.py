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

SYSTEM_PROMPT = (
    "You are a helpful assistant that summarizes podcast episodes. "
    "Given an episode title and description, write a concise 2-3 sentence summary "
    "that captures the key topics and takeaways. Be clear and engaging."
)


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
