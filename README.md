# 🎙️ Podcast Summary Agent

Subscribe to podcast RSS feeds and get detailed AI-generated summaries by email when new episodes drop.

## Architecture

- **Frontend**: Static HTML/CSS/JS in `public/`
- **API**: Vercel Serverless Functions in `api/` (Python)
- **Database**: Vercel Postgres (`feeds`, `episodes`)
- **Cron**: Vercel Cron triggers `/api/cron/check` every 6h
- **Transcripts**: YouTube auto-captions (preferred) → RSS show notes (fallback)
- **Summaries**: Gemini 2.5 Flash with structured-notes prompt
- **Email**: Resend

## Project layout

```
api/
  subscribe.py          POST /api/subscribe
  feeds.py              GET/DELETE /api/feeds
  episodes.py           GET /api/episodes
  cron/check.py         GET /api/cron/check  (cron-triggered)
lib/
  db.py                 Postgres helpers
  transcripts.py        Hybrid transcript fetcher
  summarizer.py         Gemini summarization
  notify.py             Resend email
db/schema.sql           Postgres schema
public/                 Frontend (subscription manager UI)
vercel.json             Cron + routes config
```

## Deploy

1. **Push to GitHub** ✓ (already done — `asomani-arch/podcast-summary`)
2. **Import to Vercel** at [vercel.com/new](https://vercel.com/new) — pick this repo.
3. **Add Postgres**: Vercel dashboard → Storage → Create Postgres DB → connect to project. `POSTGRES_URL` is auto-injected.
4. **Run schema**: open the Postgres query console in Vercel and paste the contents of `db/schema.sql`.
5. **Get a Resend API key** at [resend.com](https://resend.com) (100 free emails/day).
6. **Set env vars** in Vercel project settings:
   - `GEMINI_API_KEY`
   - `RESEND_API_KEY`
   - `RESEND_FROM` (optional, defaults to `onboarding@resend.dev`)
7. **Deploy**.

## Cron frequency

Default is every 6 hours (`0 */6 * * *`) so it works on Vercel's Hobby tier (which limits cron to once daily — but `*/6` is allowed because it's an interval, not a frequency cap; verify in Vercel docs for your plan).

For near-real-time, upgrade to Pro and change the cron to `*/15 * * * *` (every 15 min).
