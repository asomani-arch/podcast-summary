# Project: Podcast Summary Agent

> Auto-loaded by Claude Code in this directory. See `~/.claude/CLAUDE.md` for personal git rules.

## What this is

A web app + cron-driven agent that watches subscribed podcast RSS feeds and emails detailed AI-generated summaries (Overview, Key Topics, Takeaways, Notable Quotes, Who Should Listen) when new episodes drop.

## Live deployment

- **App**: https://podcast-summary-eight.vercel.app
- **Repo**: https://github.com/asomani-arch/podcast-summary
- **Vercel project**: `asomani-archs-projects/podcast-summary`
- **Owner email**: `asomani@wp-labs.ai`

## Stack

| Concern | Choice | Notes |
|---|---|---|
| Hosting | Vercel (Hobby tier) | Cron limited to once daily |
| Backend | FastAPI single-app at `index.py` | Vercel's modern Python runtime needs one ASGI entrypoint |
| Database | Neon Postgres (via Vercel Marketplace) | `POSTGRES_URL` injected by integration |
| LLM | Gemini 2.5 Flash (`google-genai` SDK) | User has paid account |
| Transcripts | Hybrid: YouTube auto-captions → RSS show notes fallback | `youtube-transcript-api` |
| Email | Resend | `onboarding@resend.dev` sender; only delivers to verified address until custom domain added |
| Cron | Vercel Cron, daily at 13:00 UTC | `0 13 * * *` |

## Project layout

```
index.py              FastAPI app: serves /, /api/subscribe, /api/feeds,
                      /api/episodes, /api/cron-check
lib/
  db.py               Postgres helpers
  transcripts.py      YouTube + RSS show notes
  summarizer.py       Gemini structured-notes prompt
  notify.py           Resend HTML email
public/
  index.html          Subscription manager UI
  static/             CSS + JS (uses marked.js for markdown rendering)
db/schema.sql         Postgres tables: feeds, episodes
vercel.json           Cron config only
requirements.txt      Python deps
.env                  GEMINI_API_KEY, RESEND_API_KEY, RESEND_FROM (gitignored)
.env.local            Auto-pulled DATABASE_URL/POSTGRES_URL from Neon (gitignored)
```

## Key gotchas (learned the hard way)

1. **Vercel Python runtime requires a single ASGI entrypoint** (`index.py` at root with a `FastAPI` app). The old per-file `BaseHTTPRequestHandler` pattern fails with "No python entrypoint found."
2. **Static files**: do NOT serve `public/` from FastAPI — Vercel serves them as a CDN-edge static site. FastAPI's `/` just returns a 302 redirect to `/index.html`.
3. **`feedparser` + Windows**: SSL verify failures. Always fetch with `requests.get(..., verify=False)` and pass `resp.content` to `feedparser.parse`.
4. **Hobby cron**: max 1×/day. To go faster, upgrade Vercel plan and change schedule in `vercel.json`.
5. **Resend free sender** (`onboarding@resend.dev`): only delivers to the account-owner's email. Custom domain verification needed for arbitrary recipients.
6. **Gemini model availability**: `gemini-1.5-flash` and `gemini-2.0-flash` are no longer available to new accounts; using `gemini-2.5-flash`.

## Environment variables (all set in Vercel)

| Var | Source |
|---|---|
| `GEMINI_API_KEY` | Manually set |
| `RESEND_API_KEY` | Manually set |
| `RESEND_FROM` | Manually set, default `Podcast Summary <onboarding@resend.dev>` |
| `POSTGRES_URL`, `DATABASE_URL`, etc. | Auto-injected by Neon integration |

## Common tasks

- **Local dev**: `pip install -r requirements.txt && uvicorn index:app --reload` then visit `http://localhost:8000/index.html`
- **Trigger cron manually**: `curl https://podcast-summary-eight.vercel.app/api/cron-check`
- **Apply schema to Neon**: see `db/schema.sql` — run via psql or `python -c` script (we used psycopg directly).
- **Deploy**: `vercel deploy --prod --yes` (or just push to `main` — Vercel auto-deploys via the connected GitHub repo).

## Open follow-ups / nice-to-haves

- [ ] Verify a custom domain in Resend so summaries can go to any email, not just owner's.
- [ ] Add audio-input transcription (Gemini 2.5 supports MP3 input directly) for podcasts without YouTube versions and thin show notes.
- [ ] Per-feed customization: summary length, sections to include, frequency.
- [ ] Auth (currently anyone with the URL can subscribe a feed to any email — fine for solo use, not multi-tenant).
- [ ] Surface cron-check errors in the UI (currently buried in the `errors` array of the response).
- [ ] If upgrading to Vercel Pro, change cron to `*/15 * * * *` for near-real-time.

## Conventions for this project

- Python: keep `index.py` as the single FastAPI entrypoint. Shared logic lives in `lib/`.
- Don't reintroduce the old `backend/` and `frontend/` directories — they were superseded.
- All commits should follow the global rule from `~/.claude/CLAUDE.md`: explain *why*, push to `main`, include the `Co-Authored-By` trailer.
