"""POST /api/subscribe — add a podcast feed to monitor."""
from http.server import BaseHTTPRequestHandler
import json
import requests
import feedparser

import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.db import add_feed


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or "{}")
        rss_url = body.get("rss_url")
        email = body.get("email")

        if not rss_url or not email:
            self._json(400, {"error": "rss_url and email are required"})
            return

        try:
            resp = requests.get(rss_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10, verify=False)
            feed = feedparser.parse(resp.content)
            podcast_title = feed.feed.get("title", "Unknown Podcast")
            feed_id = add_feed(rss_url, podcast_title, email)
            self._json(200, {"id": feed_id, "podcast_title": podcast_title})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
