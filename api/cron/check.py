"""GET /api/cron/check — Vercel Cron entry point.

For each active feed, finds episodes we haven't processed yet, summarizes them,
emails the summary, and records them in the DB so we never duplicate.
"""
from http.server import BaseHTTPRequestHandler
import json
import requests
import feedparser
from datetime import datetime
from time import mktime

import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lib.db import list_active_feeds, episode_exists, save_episode
from lib.transcripts import get_transcript
from lib.summarizer import summarize
from lib.notify import send_summary_email


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        results = {"feeds_checked": 0, "new_episodes": 0, "errors": []}

        for feed_row in list_active_feeds():
            results["feeds_checked"] += 1
            try:
                self._process_feed(feed_row, results)
            except Exception as e:
                results["errors"].append(f"feed {feed_row['id']}: {e}")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(results).encode())

    def _process_feed(self, feed_row: dict, results: dict):
        resp = requests.get(
            feed_row["rss_url"],
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
            verify=False,
        )
        feed = feedparser.parse(resp.content)
        podcast_title = feed.feed.get("title", feed_row["podcast_title"])

        # Only check the most recent few entries — Cron should be fast
        for entry in feed.entries[:5]:
            guid = entry.get("id") or entry.get("link") or entry.get("title")
            if not guid or episode_exists(feed_row["id"], guid):
                continue

            title = entry.get("title", "Untitled")
            description = entry.get("summary", entry.get("description", ""))
            audio_url = ""
            for enc in entry.get("enclosures", []):
                if "audio" in enc.get("type", ""):
                    audio_url = enc.get("href", "")
                    break

            published_at = None
            if entry.get("published_parsed"):
                published_at = datetime.fromtimestamp(mktime(entry.published_parsed))

            text, source = get_transcript(podcast_title, title, description)
            if not text:
                continue

            summary = summarize(podcast_title, title, text)
            send_summary_email(feed_row["email"], podcast_title, title, summary)
            save_episode(
                feed_row["id"], guid, title, published_at, audio_url, summary, source
            )
            results["new_episodes"] += 1
