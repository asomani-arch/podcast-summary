# Project: Podcast Summary Agent

> Auto-loaded by Claude Code in this directory. See `~/.claude/CLAUDE.md` for personal git rules.

## What this is

A multi-user web app that lets you search podcasts, read detailed AI summaries of any
episode, subscribe for auto-summaries of new episodes, track people/topics to catch them
across shows, and get recommendations. Built to the spec in [docs/PRD.md](docs/PRD.md).

---

## ⚠️ v5 — CURRENT ARCHITECTURE (this supersedes the older notes below)

The app was rebuilt as a multi-tenant product (Phases 0–5 of docs/PRD.md), all live on
`main` + production. Key differences from the legacy notes further down:

| Concern | v5 reality |
|---|---|
| Auth | **Supabase Auth** (email magic-link). Frontend uses supabase-js (CDN); backend verifies the bearer token via `GET {SUPABASE_URL}/auth/v1/user` (`lib/auth.py`). |
| Database | **Supabase Postgres** (transaction pooler), not Neon. Code reads `SUPABASE_DB_URL` (falls back to `POSTGRES_URL`). Schema: `db/schema_v5.sql` (multi-tenant + RLS). |
| Transcripts | **Publisher RSS transcript** (`podcast:transcript`) → **Deepgram** (full audio, `DEEPGRAM_API_KEY`) → YouTube → Gemini audio → show notes (`lib/transcripts.py`). |
| Summaries | Gemini 2.5 Flash, **thinking disabled** (else output truncates), layered investor format, ~1000 words/hr (`lib/summarizer.py`). One shared summary per episode (`episode_summaries`). |
| Search / episodes | iTunes search + RSS episode parsing (`lib/catalog.py`); no API key needed. `lib/podcastindex.py` exists but is unused (reserved for broader people-scan). |
| Delivery | `/api/scan` (every 30 min) + `/api/digest` daily/weekly, driven by **GitHub Actions** (`.github/workflows/scan.yml`), authed with `SCAN_SECRET`. One deduped delivery per (user, episode); reasons = show / person / topic. |
| People/topics | Tracked per user; scan extracts guests+topics (`lib/extract.py`) from subscribed shows + a rotating batch of ~24 curated `is_popular` shows (seed via `POST /api/admin/seed-popular`). |
| Recommendations | `/api/recommendations` — episodes matching tracked people/topics not yet delivered. |
| Deploy | `git push origin <branch>:main` → Vercel auto-deploys production. Or API deploy with `VERCEL_TOKEN`. Local secrets in `.env.local` (gitignored) incl. `VERCEL_TOKEN`, `SCAN_SECRET`. |

**v5 env vars (Vercel):** `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`,
`SUPABASE_DB_URL`, `DEEPGRAM_API_KEY`, `SCAN_SECRET`, `GEMINI_API_KEY`, `RESEND_API_KEY`,
`RESEND_FROM`. (Legacy Neon `POSTGRES_URL*` vars are still present but unused.)

**Known limitations:** email only delivers to the owner until a custom Resend domain is
verified — everyone else reads summaries in the in-app **My Summaries** view (PRD §8).
People/topic coverage = subscribed + curated popular shows (add a free Podcast Index key
for true universe scan).

### Product feedback round 1 (2026-06-22)

Test-user feedback incorporated this round (see git log). Highlights:
- **Brand is "PodcastAI"** across UI + emails (decided, not generic).
- **Sign-in screen** (the logged-out homepage = the auth overlay) now has a value-prop
  tagline, privacy fine print, a © footer, and says "secure sign-in link" (not "magic
  link") with a 1-hour expiry note.
- **Header**: logo links home; the raw email is replaced by a profile-avatar dropdown
  (Gravatar w/ initial fallback) → email · My Summaries · My Subscriptions · Sign Out.
  Nav "Discover" → "For You"; "Inbox" → "My Summaries"; tray sections relabeled
  Podcasts / Guests / Topics.
- **Summaries**: ad/sponsor reads are now excluded from the brief (summarizer prompt +
  `SUMMARY_STYLE_VERSION` bumped to `pe-layered-v6`, so cached summaries regenerate on
  next view). Share control (Copy link / Email) added to the summary panel, backed by
  `?ep=<episode_id>` deep-links.
- **Email branding/deliverability**: digest + summary emails rebranded to PodcastAI with
  privacy + © footer. Branded *auth* emails and real delivery to non-owners require a
  verified sending domain — **dashboard/DNS steps are in [docs/EMAIL_SETUP.md](docs/EMAIL_SETUP.md)**
  (Resend domain → Vercel `RESEND_FROM` → Supabase custom SMTP + email templates).
- **Catalog coverage**: search already covers the whole iTunes catalog (any show, incl.
  *A Slight Change of Plans*). The *curated* people/topic scan list (`POPULAR_SHOW_NAMES`)
  was broadened beyond business/tech. **Re-run `POST /api/admin/seed-popular`** (with the
  `X-Scan-Secret` header) after deploy so the new shows are monitored.

**Deferred (scoped follow-up):** *Audio/verbal summaries* — TTS versions of each brief
with a written/audio toggle. Needs a TTS provider (e.g. Gemini TTS / ElevenLabs), audio
storage (Vercel Blob), a player UI, and per-generation cost handling. Not built this round.

### Product feedback round 2 (2026-06-22) — titratable summary detail

Users can now set a **summary detail level — Quick / Standard / Deep** — in the
Subscriptions tray (saved preference, like cadence). It applies to summaries they open
**and** to what's delivered to them (email/digest/inbox).
- `summarizer.py`: `detail_level` drives length (×0.35 / ×1.0 / ×1.8 of the duration
  baseline) and section depth — Quick = TL;DR + takeaways only; Deep = exhaustive. Standard
  is byte-for-byte the old behavior.
- Summaries are now cached **per (episode, detail_level)**; `profiles.summary_detail` holds
  the preference. The scan generates one summary per distinct level among matched users
  (one shared transcript fetch) and delivers each user their level.
- **⚠️ Requires a DB migration** (in `db/schema_v5.sql`): adds `episode_summaries.detail_level`
  + `profiles.summary_detail`, and swaps the `episode_summaries` unique key from `(episode_id)`
  to `(episode_id, detail_level)`. The new code's `ON CONFLICT (episode_id, detail_level)`
  needs this, so **run the migration before/with the deploy**, not after.

---

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
| Podcast search | Apple iTunes Search API | No auth required; returns title / publisher / artwork / feedUrl |
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

> `PODCAST_INDEX_KEY` / `PODCAST_INDEX_SECRET` were used by v2's search backend.
> Search now uses the unauthenticated iTunes Search API, so these vars are unused
> and can be deleted from Vercel — they aren't read anywhere in the code.

## Common tasks

- **Local dev**: `pip install -r requirements.txt && uvicorn index:app --reload` then visit `http://localhost:8000/index.html`
- **Trigger cron manually**: `curl https://podcast-summary-eight.vercel.app/api/cron-check`
- **Apply schema to Neon**: see `db/schema.sql` — run via psql or `python -c` script (we used psycopg directly).
- **Deploy**: `vercel deploy --prod --yes` (or just push to `main` — Vercel auto-deploys via the connected GitHub repo).

## v3 additions (May 2026)

- **Per-feed customization**: `feeds.summary_length` ('short'|'standard'|'deep'), `feeds.sections` (TEXT[]), `feeds.frequency_days` (INT). Edited via `PATCH /api/feeds/{id}`. Summarizer prompt is composed from selected sections, length controls Gemini `max_output_tokens` and depth instructions.
- **Cron frequency gating**: cron skips a feed when `last_delivered_at + frequency_days` is still in the future. Lets weekly/monthly feeds coexist with daily ones on a single daily cron tick.
- **Cron run log**: every `/api/cron-check` invocation writes a row to `cron_runs` (feeds_checked, feeds_skipped, new_episodes, errors[], ok). `GET /api/status` returns the latest 5 runs.
- **Tray UI**: status banner at top of subscriptions tray shows last-run summary + any errors. Each subscription has a gear icon that toggles an inline settings drawer (length / sections / frequency).

### Resend custom domain — manual steps

Until done, summary emails only deliver to the account owner (asomani@wp-labs.ai). To enable delivery to any recipient:

1. In the Resend dashboard, add a domain you control (e.g. `podcastai.<yourdomain>`).
2. Add the DNS records Resend provides (SPF + DKIM + optional MX) at your DNS host.
3. Wait for verification (usually minutes).
4. Update the `RESEND_FROM` env var in Vercel to `Podcast Summary <hello@<yourdomain>>`.
5. Redeploy (or trigger a new deploy via push) for the env var to take effect.

No code change is required — `lib/notify.py` already reads `RESEND_FROM` from env.

## Open follow-ups / nice-to-haves

- [ ] Verify a custom domain in Resend so summaries can go to any email, not just owner's (manual steps above).
- [ ] Add audio-input transcription (Gemini 2.5 supports MP3 input directly) for podcasts without YouTube versions and thin show notes.
- [ ] Auth (currently anyone with the URL can subscribe a feed to any email — fine for solo use, not multi-tenant).
- [ ] If upgrading to Vercel Pro, change cron to `*/15 * * * *` for near-real-time. Frequency gating already in place so feeds with higher `frequency_days` are still skipped.

## Schema migrations

`db/schema.sql` is idempotent — all `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. After deploying schema changes, run the file against Neon. Example:

```powershell
$env:POSTGRES_URL = (vercel env pull --environment=production .env.local | Out-Null; (Get-Content .env.local | Select-String 'POSTGRES_URL').ToString().Split('=',2)[1].Trim('"'))
psql $env:POSTGRES_URL -f db/schema.sql
```

Or from Python: `python -c "import psycopg, pathlib, os; psycopg.connect(os.environ['POSTGRES_URL']).execute(pathlib.Path('db/schema.sql').read_text()).connection.commit()"`

## Conventions for this project

- Python: keep `index.py` as the single FastAPI entrypoint. Shared logic lives in `lib/`.
- Don't reintroduce the old `backend/` and `frontend/` directories — they were superseded.
- All commits should follow the global rule from `~/.claude/CLAUDE.md`: explain *why*, push to `main`, include the `Co-Authored-By` trailer.
