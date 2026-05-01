"""GET /api/feeds — list subscribed feeds. DELETE /api/feeds?id=N — unsubscribe."""
from http.server import BaseHTTPRequestHandler
import json
from urllib.parse import urlparse, parse_qs

import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.db import get_conn


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("""
                    SELECT f.id, f.rss_url, f.podcast_title, f.email, f.created_at,
                           COUNT(e.id) AS episode_count
                    FROM feeds f
                    LEFT JOIN episodes e ON e.feed_id = f.id
                    WHERE f.active = TRUE
                    GROUP BY f.id
                    ORDER BY f.created_at DESC
                """)
                rows = cur.fetchall()
                # serialize datetimes
                for r in rows:
                    if r.get("created_at"):
                        r["created_at"] = r["created_at"].isoformat()
            self._json(200, {"feeds": rows})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def do_DELETE(self):
        qs = parse_qs(urlparse(self.path).query)
        feed_id = qs.get("id", [None])[0]
        if not feed_id:
            self._json(400, {"error": "id query param required"})
            return
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("UPDATE feeds SET active = FALSE WHERE id = %s", (feed_id,))
            self._json(200, {"ok": True})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload, default=str).encode())
