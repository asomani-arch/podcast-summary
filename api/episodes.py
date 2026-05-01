"""GET /api/episodes?feed_id=N — list summarized episodes (or all if no feed_id)."""
from http.server import BaseHTTPRequestHandler
import json
from urllib.parse import urlparse, parse_qs

import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.db import get_conn


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        feed_id = qs.get("feed_id", [None])[0]
        try:
            with get_conn() as conn, conn.cursor() as cur:
                if feed_id:
                    cur.execute("""
                        SELECT e.*, f.podcast_title FROM episodes e
                        JOIN feeds f ON f.id = e.feed_id
                        WHERE e.feed_id = %s
                        ORDER BY e.published_at DESC NULLS LAST LIMIT 50
                    """, (feed_id,))
                else:
                    cur.execute("""
                        SELECT e.*, f.podcast_title FROM episodes e
                        JOIN feeds f ON f.id = e.feed_id
                        ORDER BY e.published_at DESC NULLS LAST LIMIT 50
                    """)
                rows = cur.fetchall()
                for r in rows:
                    for k in ("published_at", "emailed_at", "created_at"):
                        if r.get(k):
                            r[k] = r[k].isoformat()
            self._json(200, {"episodes": rows})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload, default=str).encode())
