# PRD — Podcast Intelligence (v5)

> Status: **Draft for review.** Authored from a structured interview on 2026-06-16.
> This is the spec we execute against. Decisions marked **[DEFAULT]** were not
> explicitly chosen in the interview — I picked a sensible default; override any
> you disagree with before we build.

---

## 1. Vision

A multi-user web app where a user can:

1. **Search** for podcasts and browse / search a show's historical episodes.
2. Open any episode and get a **detailed, investor-grade summary** — not a high-level
   blurb, but enough depth to "not miss anything" while staying skimmable.
3. **Subscribe** to a show and automatically receive a summary of every new episode
   by email.
4. **Track specific people** — get a summary whenever a tracked person appears as a
   guest on *any* (popular) podcast, not just shows the user follows.
5. **Track specific topics** — get a summary whenever an episode substantially covers
   a tracked topic.
6. Receive **recommendations** for new podcasts/episodes based on the *people* and
   *topics* they've shown interest in (interest is modeled at the guest/topic level,
   not the show level).

The existing prototype already does podcast search, on-demand summarization, single-
feed subscriptions, and daily-cron email for a single owner. v5 turns it into a
**multi-tenant product** and adds people-tracking, topic-tracking, and recommendations.

---

## 2. Target user & persona

**Primary persona:** a private-equity investment professional who wants to stay on top
of relevant podcasts but has very limited time. They want to *not miss anything
meaningful* yet be able to triage in seconds.

This persona drives two cross-cutting requirements:

- **Investor lens** — summaries are written through a PE/investor frame (deal sourcing,
  diligence, underwriting, value creation, management quality, industry structure,
  competitive advantage, unit economics, capital allocation, regulatory risk, exit
  implications). This is retained from the current `pe-newsletter` prompt.
- **Layered for triage** — every summary front-loads a 10-second read (TL;DR + bottom-
  line takeaways) and puts the exhaustive walkthrough below it.

---

## 3. Decision log (from interview)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Audience | **Multi-user product** from day one (real accounts, per-user isolation) |
| 2 | Summary lens | **PE / investor lens** retained as the default voice |
| 3 | Summary depth | **~1,000 words per hour** of audio, scaled by duration (floor ~400, soft cap ~3,000), in a **layered** structure |
| 4 | People-tracking scope | **Broad scan over popular shows** (no long tail) |
| 5 | Detection backbone | **Podcast Index API** (free, full-text episode + person search) |
| 6 | Recommendation granularity | **(guest, topic) level**, recommend *episodes* across shows — not show-level |
| 7 | Summary model | **Gemini 2.5 Flash** for summaries (audio transcription also Gemini) |
| 8 | Delivery channel | **Email-first**; in-app is for search/subscribe/manage (+ a minimal in-app summary view, see §8) |
| 9 | Email cadence | **Per-user setting**, default **instant per-episode**; options: instant / daily digest / weekly digest |
| 10 | People summary scope | **Full episode, framed around the tracked person** |
| 11 | Data backbone | **Supabase** — Postgres + Auth + Row-Level Security; **retire Neon** |
| 12 | Auth | **Supabase Auth** (Google + email OAuth) |
| 13 | Scheduling | **External scheduler on Vercel Hobby** (Supabase `pg_cron` + `pg_net`, or GitHub Actions) hitting our endpoints frequently |
| 14 | Topic tracking | **Yes** — topics are trackable like people |
| 15 | Custom email domain | **Tabled** — no domain yet; multi-user email delivery deferred (see §8 + §13) |

---

## 4. Goals & non-goals

### Goals
- Detailed, faithful, investor-framed summaries that scale with episode length.
- Three subscription primitives that all converge on the same delivery pipeline:
  **show**, **person**, **topic**.
- Recommendations driven by demonstrated interest in people and topics.
- Clean multi-tenant data isolation (RLS), low-friction OAuth login.
- Stay on free/cheap infra (Vercel Hobby + Supabase free tier + free Podcast Index).

### Non-goals (this version)
- Long-tail / obscure podcast coverage (popular shows only).
- Real-time (sub-minute) delivery — "instant" means "next scan tick," ~15–30 min.
- An audio player / listening experience — we summarize, we don't stream.
- Per-user *lens* customization — the lens is fixed PE for everyone (this is what lets
  us cache one summary per episode globally; see §7).
- Custom email domain / arbitrary-recipient delivery (deferred, §13).

---

## 5. Functional requirements

### F1 — Accounts & auth
- Sign up / sign in via **Supabase Auth** (Google OAuth + email).
- Every user-owned row is scoped by `user_id = auth.uid()` and protected by RLS.
- A `profiles` row is created on first login holding per-user preferences
  (email cadence, default delivery channel, etc.).
- Onboarding: after first login, prompt the user to add at least one show / person /
  topic so recommendations and delivery have something to work with. **Skippable** —
  never blocks access. *(Resolved.)*

### F2 — Podcast search & discovery
- Search popular podcasts by name/keyword. **Migrate from iTunes to Podcast Index**
  as the primary catalog source (full-text search, stable IDs, episode-level search).
  Keep iTunes as a fallback/enrichment source for artwork if useful.
- Results show title, publisher, artwork, description, episode count, and whether the
  current user already follows the show.

### F3 — Episode browsing & back-catalog search
- For a selected show, list episodes (newest first) with pagination — backed by
  Podcast Index episode listings, not just the truncated RSS window the prototype uses.
- **Search within a show's back catalog** by keyword (title/description) — satisfies
  "search for a certain historical episode."
- Each episode row indicates whether a cached summary exists; clicking summarizes
  on demand if not.

### F4 — On-demand detailed summary
- Generate (or return cached) a summary for any episode. See **§7** for the format and
  length spec.
- Summaries are cached **globally per episode** (not per user) because the lens is fixed.
- Per-user soft cap of **4 newly-generated (uncached) summaries/user/day** on
  **manual on-demand** summarization (browsing the back catalog). Cached reads are
  unlimited, and automated subscription/person/topic deliveries do **not** count against
  this cap. Sized for a time-poor PE reader, not a power-scraper. *(Resolved.)*

### F5 — Show subscriptions
- Follow / unfollow a show. A follow means: summarize every new episode and deliver it
  per the user's cadence.
- Per-subscription override of cadence (e.g. one show is "instant," the rest "daily").
- Retains the existing per-feed knobs where still meaningful (length is now duration-
  driven, so the manual length knob is dropped; section selection is dropped in favor
  of the fixed layered format).

### F6 — People tracking
- Add a person by name (with disambiguation when multiple people match).
- When a tracked person is detected as a guest on any scanned (popular or subscribed)
  show, summarize that episode **framed around the person** and deliver it.
- The summary leads with a "**Why you're getting this:** *[Person]* appeared on
  *[Show]*" banner and emphasizes that person's contributions in the walkthrough.

### F7 — Topic tracking
- Add a topic (free text, e.g. "private credit", "AI infrastructure").
- When an episode is detected to **substantially** cover a tracked topic (confidence
  threshold, not a keyword hit), summarize and deliver it with a "matched topic" banner.
- **[DEFAULT]** Topic matching uses LLM relevance scoring over episode metadata
  (title/description) with a configurable threshold to suppress weak matches.

### F8 — Recommendations
- Model user interest at the **(person, topic)** level from explicit signals only for
  v5: tracked people, tracked topics, and followed shows. (Engagement signals such as
  opens/reads are **deferred** — see §14.)
- Recommend **episodes** (across any popular show) whose extracted guests/topics match
  the interest profile — explicitly handling the "Rogan has Elon one week, an unrelated
  guest the next" case by scoring at the episode level, not the show level.
- Each recommendation carries a reason ("because you track *Elon Musk*", "covers
  *AI infrastructure*"). One-click: subscribe to the show, track the person, or open
  the summary.
- Surfaced in an in-app **Discover** view and as a small "Recommended for you" block
  (**~2–3 picks**) appended to digests. *(Resolved.)*

### F9 — Email delivery
- Cadence is a per-user setting (default **instant**), with options instant / daily /
  weekly, plus per-subscription override.
- **Dedup:** at most **one email per episode per user**, even when an episode matches
  multiple of their interests (show + person + topic). The email lists all match
  reasons. (See §10.)
- Digest emails batch all qualifying episodes since the last send, newest first,
  grouped by reason.

### F10 — In-app management
- Manage subscriptions, tracked people, tracked topics, and cadence preferences.
- Status panel showing recent scan/cron runs and errors (retained from prototype).
- A minimal **in-app summary reader** so summaries are viewable even while email
  delivery is owner-only (see §8 note + §13).

---

## 6. Summary format & length spec

### Length
```
target_words = clamp( round(1000 * duration_hours), 400, 3000 )
# duration unknown -> assume 60 min -> 1000 words
```
Examples: 25 min → ~420; 1 h → ~1,000; 1 h 45 m → ~1,750; 3 h+ → capped 3,000.

### Structure (fixed, layered — PE lens throughout)
1. **Header** — show, episode title, date, duration, guest(s), transcript-source note
   if the transcript was not full.
2. **(Conditional) "Why you're getting this"** banner — for person/topic-triggered
   deliveries.
3. **TL;DR** — 2–3 sentences: the single most investor-relevant thing in the episode.
   *(10-second read.)*
4. **Bottom-line takeaways** — 3–5 bold bullets, each a punchy investor-relevant
   conclusion. *(30-second read.)*
5. **Detailed walkthrough** — thematic / segment-by-segment subheadings covering every
   meaningful thread, argument, and example. This is where the ~1,000 words/hour lives.
   *(Full read.)* For person-triggered summaries, the person's contributions are
   emphasized here.
6. **(Optional) Notable quotes** — 1–3 verbatim, attributed, only when genuinely strong.
7. **(Optional) Companies / sectors / numbers mentioned** — structured list, only when
   present in the source.

### Rules (retained from current prompt)
- Transcript is the source of truth; never fabricate. If only show notes are available,
  say so explicitly and don't infer beyond the evidence.
- Mention companies/sectors/numbers only when present in the source.
- Avoid hype, filler, and generic recap language.

---

## 7. Architecture

### 7.1 Stack
| Concern | Choice |
|---|---|
| Hosting | Vercel (Hobby), single FastAPI ASGI app at `index.py` |
| DB + Auth | **Supabase** (Postgres + Auth + RLS), replaces Neon |
| Catalog/search | **Podcast Index API** (replaces iTunes as primary) |
| Summaries | Gemini 2.5 Flash |
| Transcripts | Colossus (authed) → YouTube captions → Gemini audio → show notes (retained) |
| Guest/topic extraction | `<podcast:person>` tags where present + Gemini extraction over title/description (and transcript when summarizing) |
| Email | Resend (owner-only until a domain is verified — §13) |
| Scheduling | External trigger (Supabase `pg_cron`+`pg_net` or GitHub Actions) → our endpoints |

### 7.2 Data model (Supabase / Postgres)

**Global tables** (read-all-authenticated, write service-role only):
- `podcasts` — `id`, `pi_feed_id`, `itunes_id`, `rss_url` (unique), `title`,
  `publisher`, `artwork_url`, `description`, `categories[]`, `is_popular` (bool — in the
  curated scan set), `last_scanned_at`.
- `episodes` — `id`, `podcast_id`, `pi_episode_id`, `guid`, `title`, `description`,
  `audio_url`, `episode_url`, `published_at`, `duration_seconds`. `UNIQUE(podcast_id, guid)`.
- `episode_summaries` — `id`, `episode_id` (unique), `summary_md`, `tldr`,
  `target_words`, `transcript_source`, `model`, `style_version`, `created_at`. *(One
  shared summary per episode — fixed lens makes this safe.)*
- `people` — `id`, `name`, `normalized_name`, `bio`, `external_ids` (jsonb).
- `episode_people` — `episode_id`, `person_id`, `confidence`, `source` (`pi_tag`|`llm`).
- `episode_topics` — `episode_id`, `topic`, `confidence`.

**User-scoped tables** (RLS: `user_id = auth.uid()`):
- `profiles` — `user_id` (PK, FK→`auth.users`), `email`, `default_cadence`
  (`instant`|`daily`|`weekly`), `created_at`.
- `subscriptions` — `user_id`, `podcast_id`, `cadence_override`, `created_at`.
- `tracked_people` — `user_id`, `person_id`, `created_at`.
- `tracked_topics` — `user_id`, `topic`, `created_at`.
- `deliveries` — `user_id`, `episode_id`, `reasons` (jsonb: e.g. `[{type:"show"},
  {type:"person",person_id},{type:"topic",topic}]`), `status`
  (`queued`|`sent`|`failed`), `channel`, `created_at`, `sent_at`.
  **`UNIQUE(user_id, episode_id)`** — the dedup backbone (F9).
- `engagement` *(optional, for recs)* — `user_id`, `episode_id`, `action`
  (`open`|`summarize`|`read`), `created_at`.

**Ops:**
- `scan_runs` — successor to `cron_runs`: per-tick `started_at`, `shows_scanned`,
  `episodes_ingested`, `episodes_matched`, `summaries_generated`, `emails_sent`,
  `errors` (jsonb), `ok`.

### 7.3 Cost-control architecture (important)
Scanning every popular show and summarizing every episode would be expensive.
The pipeline keeps expensive work proportional to *demand*, not to the catalog:

1. **Cheap detection first.** Each scan tick pulls *new episode metadata* (title/desc/
   person-tags) from the union of {subscribed shows} ∪ {curated popular shows}. Run
   cheap extraction (person tags + lightweight LLM/string match) to get
   `episode_people` / `episode_topics`. No transcript, no full summary yet.
2. **Match against the global interest set.** Compute which users want this episode
   (subscribed show, tracked person present, or tracked topic covered). If **zero**
   users match and no one requested it on-demand → **stop** (don't summarize).
3. **Summarize once, on demand.** Only matched episodes get the transcript+summary
   pipeline, cached globally in `episode_summaries`.
4. **Fan out deliveries.** Create one `deliveries` row per matching user (dedup via the
   unique constraint), then send/queue per cadence.

### 7.4 Scheduling (Hobby-compatible)
- `/api/scan` — frequent tick (~every 15–30 min): ingest → detect → match →
  summarize-if-matched → instant deliveries. Triggered by Supabase `pg_cron` + `pg_net`
  (preferred — stays in Supabase) or a GitHub Actions cron workflow.
- `/api/digest?cadence=daily` — once daily: batch+send daily digests.
- `/api/digest?cadence=weekly` — once weekly: weekly digests.
- Endpoints protected by a shared secret header so only the scheduler can invoke them.
- **Risk:** audio transcription of 2–3 h episodes may approach the function timeout.
  Mitigation: prefer caption/transcript sources; cap/stream audio (already done); if
  needed, move transcription to a queued/background path later.

---

## 8. Email-delivery caveat (no domain yet)

Decision #15 tabled the custom domain. Consequences for v5:
- Resend's free sender (`onboarding@resend.dev`) only delivers to the **account owner**
  (`asomani@wp-labs.ai`). So **email to arbitrary users won't work yet.**
- Therefore the **in-app summary reader (F10) is required**, not optional, even though
  the product is "email-first": it's how non-owner users see summaries until a domain
  exists.
- Everything is built domain-ready: flipping `RESEND_FROM` to a verified domain later
  turns on multi-user email with no code change. The verification steps are already
  documented in `CLAUDE.md`.

---

## 9. Detection & matching details

- **Guests:** prefer `<podcast:person>` namespace tags from Podcast Index; fall back to
  Gemini name-extraction from title/description; resolve to canonical `people` rows via
  normalized-name matching (with a disambiguation step on add).
- **Topics:** Gemini extracts a short topic list per episode; topic *tracking* matches
  via embedding/LLM relevance against the episode, gated by a confidence threshold to
  avoid noisy matches.
- **Dedup:** the `UNIQUE(user_id, episode_id)` constraint guarantees one delivery per
  episode per user; the `reasons` array records *every* reason it matched so the email
  can say "matched because: you follow *Invest Like the Best*, and *Elon Musk* appeared."
- Negative feedback ("not relevant"): **deferred to post-v5** (see §14). The
  `deliveries` / match schema is designed so it slots in later without migration.

---

## 10. Migration plan (Neon → Supabase, prototype → v5)

1. Provision Supabase project; enable Google + email auth.
2. Author the new schema (§7.2) as idempotent SQL + RLS policies.
3. Port `lib/db.py` from `POSTGRES_URL`/Neon to the Supabase connection string; add a
   Supabase-JWT verification dependency in FastAPI; use the service-role key for global
   writes (scan pipeline) and enforce `user_id` scoping for user routes (RLS as backstop).
4. Refactor existing endpoints:
   - `feeds` → `subscriptions` + global `podcasts`.
   - per-episode `episodes.summary` → global `episode_summaries`.
   - `cron-check` → `/api/scan` + `/api/digest`.
5. Migrate the prototype's minimal data (current feeds/episodes) into the new shape,
   or accept a clean start (data is personal and small).
6. Swap iTunes → Podcast Index for search + episode listing; keep `verify=False` RSS
   fetch pattern and the transcript resolver as-is.

---

## 11. Phased delivery

- **Phase 0 — Foundation:** Supabase (schema + RLS + auth), FastAPI auth wiring, port
  DB layer, adopt Podcast Index, global summary cache. *(No behavior change for the
  owner; everything still works, now multi-tenant-ready.)*
- **Phase 1 — Detailed summaries:** layered format + duration-scaled length. Highest-
  value quality upgrade; independent of the rest.
- **Phase 2 — Subscriptions & delivery:** show follow/unfollow, scan pipeline, instant
  + digest delivery, per-user cadence, dedup, in-app reader.
- **Phase 3 — People tracking:** guest extraction, matching, person-framed summaries.
- **Phase 4 — Topic tracking:** topic extraction + threshold matching.
- **Phase 5 — Recommendations:** (person, topic) interest model (explicit signals only;
  engagement deferred) + Discover view + digest block.
- **Phase 6 — (Deferred) Custom domain:** verify a domain in Resend → enable multi-user
  email.

---

## 12. Resolved defaults

The five open `[DEFAULT]`s from the draft are now decided (2026-06-16):

1. **On-demand summary cap — 4 newly-generated/user/day.** Manual back-catalog
   summarization only; cached reads unlimited; automated subscription/person/topic
   deliveries are exempt. Sized for a time-poor PE reader.
2. **Onboarding seeds ≥1 interest — yes, skippable** (never blocks access).
3. **Recs surfaced in Discover + digest — yes**, with the digest block capped at ~2–3
   picks.
4. **Engagement as a rec signal — deferred** (§14). v5 recommends on explicit signals
   only: tracked people, tracked topics, followed shows.
5. **"Not relevant" feedback loop — deferred to post-v5** (§14); schema kept compatible.

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| No email domain → can't email non-owner users | In-app reader (F10); domain-ready config; Phase 6 |
| Long-episode audio transcription hits function timeout | Prefer captions; cap/stream audio; queue later if needed |
| Guest/topic extraction false +/− | `<podcast:person>` tags + confidence thresholds + "not relevant" feedback |
| Scan cost across popular shows | Metadata-only detection before summarizing (§7.3) |
| Podcast Index rate limits | Cache catalog/episode data; bounded curated popular set |
| RLS + Python service-role footguns | Service-role only in scan pipeline; user routes always filter by `auth.uid()` |
| Gemini quality ceiling on dense 3 h episodes | Revisit Claude-via-AI-Gateway for summaries if fidelity is insufficient |

---

## 14. Deferred / future

- **Engagement-based recommendations** — use open/read/summarize events to refine the
  (person, topic) interest model. Deferred from v5 (sparse signal early + extra tracking
  surface). The `engagement` table exists in the schema (§7.2) so it can be populated
  later without migration.
- **"Not relevant" feedback loop** — per-user suppression + match tuning. Deferred;
  `deliveries`/match schema kept compatible.
- **Custom email domain (Phase 6)** — enables arbitrary-recipient email. Until then,
  email is owner-only and the in-app reader carries non-owner users (§8, §13).
- **Per-user lens customization** — the lens is fixed PE for now (this is what lets us
  cache one summary per episode globally). Revisit only if non-PE users appear.
- **Claude-via-AI-Gateway summaries** — fallback if Gemini fidelity proves insufficient
  on long, dense episodes.
