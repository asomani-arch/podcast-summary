# Email setup — branded sending + auth emails

This covers the two email problems your test user hit:

1. **She never received any emails.** Today the app sends from Resend's shared
   `onboarding@resend.dev` address, which Resend only delivers to *your own*
   account email. Everyone else gets nothing. Fixing this needs a verified
   sending domain.
2. **Auth emails look like spam** — they come "From: **Supabase Auth**" with terse
   copy. Supabase sends those, so the fix is in the Supabase dashboard: point it at
   your domain and rewrite the templates.

Everything below is a **dashboard / DNS task** — there's no code to change. The
code already reads `RESEND_FROM` and the brand copy is in place. Do Part A first
(it unlocks both B and C).

Times are approximate; DNS can take a few minutes to a few hours to propagate.

---

## Part A — Verify a sending domain in Resend

You'll send from a subdomain you control. Using a subdomain (e.g.
`mail.wp-labs.ai`) instead of the root keeps podcast email separate from your
normal mail and is the Resend-recommended setup.

1. Log in to **https://resend.com** with the account that owns `RESEND_API_KEY`.
2. Go to **Domains → Add Domain**. Enter a subdomain, e.g. `mail.wp-labs.ai`
   (replace `wp-labs.ai` with the domain you actually control). Click **Add**.
3. Resend shows a list of **DNS records** (usually 3–4): an `MX` record, one or
   two `TXT` records for SPF/DKIM, and an optional `TXT` for DMARC. Leave this
   page open.
4. In your DNS host (wherever `wp-labs.ai`'s DNS lives — e.g. Cloudflare, GoDaddy,
   Namecheap), add **each record exactly as shown** (name, type, value). Copy/paste
   the values to avoid typos.
5. Back in Resend, click **Verify DNS Records**. It may say "pending" for a few
   minutes — recheck until every record shows a green check.
6. Once verified, you can send from any address at that subdomain, e.g.
   `PodcastAI <hello@mail.wp-labs.ai>` or `PodcastAI <notifications@mail.wp-labs.ai>`.

### Then point the app at it

7. In **Vercel → your project → Settings → Environment Variables**, edit
   `RESEND_FROM` (Production) to:

   ```
   PodcastAI <hello@mail.wp-labs.ai>
   ```

   (use the address from step 6).
8. **Redeploy** so the new value takes effect (push any commit, or Vercel →
   Deployments → ⋯ → Redeploy). Digest and instant summary emails will now reach
   any recipient, from your brand.

---

## Part B — Make Supabase auth emails send through your domain

This is what changes the "From: Supabase Auth" to "From: PodcastAI" and lets the
confirm/sign-in emails actually land in inboxes.

1. In **Supabase → your project → Authentication → Emails → SMTP Settings**,
   toggle **Enable Custom SMTP** on.
2. Fill in (these are Resend's SMTP details):
   - **Host:** `smtp.resend.com`
   - **Port:** `465`
   - **Username:** `resend`
   - **Password:** your `RESEND_API_KEY` value
   - **Sender email:** `hello@mail.wp-labs.ai` (the verified address from Part A)
   - **Sender name:** `PodcastAI`
3. Save. Send yourself a test sign-in to confirm it arrives from **PodcastAI**.

---

## Part C — Rewrite the two auth email templates

In **Supabase → Authentication → Emails → Templates**, edit these two. Set both
the **Subject** and the **Message body** (switch the body editor to HTML / "Source"
and paste the blocks below). The `{{ .ConfirmationURL }}` token is filled in by
Supabase automatically — leave it as-is.

> **Prerequisite:** Supabase now **gates ALL auth-email customization behind custom
> SMTP.** The dashboard shows "Set up custom SMTP to edit templates", and the default
> email service ignores custom subjects/bodies. So Parts A + B below are **required
> first** — you cannot brand the subject, body, *or* sender until a verified sending
> domain + custom SMTP are configured. There is no no-domain branding path.
> (Don't use Resend's `onboarding@resend.dev` as the SMTP sender to dodge this — it
> only delivers to your own Resend account email, so your test users would stop
> receiving links entirely.)

### First, set the link lifetime so the copy is accurate

In **Authentication → Emails** (or **Providers → Email**), find **Email OTP
Expiration** and set it to **3600** seconds (1 hour). The templates below say
"expires in 1 hour" to match — if you choose a different value, update the wording.

### Template 1 — "Confirm signup" (first-ever sign-in)

**Subject:**

```
Confirm your email to start using PodcastAI 🎙️
```

**Message body:**

```html
<div style="margin:0;padding:24px 12px;background:#f4f4f5;font-family:'Inter',-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <div style="max-width:480px;margin:0 auto;background:#ffffff;border:1px solid #ececec;border-radius:14px;overflow:hidden;">
    <div style="height:4px;background:#f59e0b;"></div>
    <div style="padding:28px 32px;">
      <div style="font-size:20px;font-weight:700;letter-spacing:-0.02em;color:#111111;margin-bottom:22px;">🎙️ Podcast<span style="color:#f59e0b;">AI</span></div>
      <h1 style="font-size:22px;line-height:1.3;color:#111111;margin:0 0 12px;">Welcome to PodcastAI</h1>
      <p style="font-size:15px;line-height:1.6;color:#3f3f46;margin:0;">Thank you for joining the PodcastAI community to receive summaries of your favorite podcasts. Confirm your email address to finish signing up.</p>
      <p style="margin:26px 0;">
        <a href="{{ .ConfirmationURL }}" style="background:#f59e0b;color:#111111;text-decoration:none;font-weight:700;font-size:15px;padding:13px 26px;border-radius:8px;display:inline-block;">Confirm your email address</a>
      </p>
      <p style="font-size:13px;line-height:1.6;color:#6b7280;margin:0;">This link expires in 1 hour and can only be used once. If you didn’t sign up for PodcastAI, you can safely ignore this email.</p>
    </div>
    <div style="padding:16px 32px;border-top:1px solid #eeeeee;background:#fafafa;">
      <p style="font-size:12px;line-height:1.5;color:#9ca3af;margin:0;">© 2026 Ashutosh Somani · We never sell your data or send spam.</p>
    </div>
  </div>
</div>
```

### Template 2 — "Magic Link" (signing in again after signing out)

**Subject:**

```
Your PodcastAI sign-in link 🎙️
```

**Message body:**

```html
<div style="margin:0;padding:24px 12px;background:#f4f4f5;font-family:'Inter',-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <div style="max-width:480px;margin:0 auto;background:#ffffff;border:1px solid #ececec;border-radius:14px;overflow:hidden;">
    <div style="height:4px;background:#f59e0b;"></div>
    <div style="padding:28px 32px;">
      <div style="font-size:20px;font-weight:700;letter-spacing:-0.02em;color:#111111;margin-bottom:22px;">🎙️ Podcast<span style="color:#f59e0b;">AI</span></div>
      <h1 style="font-size:22px;line-height:1.3;color:#111111;margin:0 0 12px;">Welcome back to PodcastAI</h1>
      <p style="font-size:15px;line-height:1.6;color:#3f3f46;margin:0;">Your podcast summaries await. Click the button below to sign in — no password needed.</p>
      <p style="margin:26px 0;">
        <a href="{{ .ConfirmationURL }}" style="background:#f59e0b;color:#111111;text-decoration:none;font-weight:700;font-size:15px;padding:13px 26px;border-radius:8px;display:inline-block;">Sign in to PodcastAI</a>
      </p>
      <p style="font-size:13px;line-height:1.6;color:#6b7280;margin:0;">This link expires in 1 hour and can only be used once. If you didn’t request it, you can safely ignore this email.</p>
    </div>
    <div style="padding:16px 32px;border-top:1px solid #eeeeee;background:#fafafa;">
      <p style="font-size:12px;line-height:1.5;color:#9ca3af;margin:0;">© 2026 Ashutosh Somani · We never sell your data or send spam.</p>
    </div>
  </div>
</div>
```

> Note: Supabase shares one OTP-expiry setting across the auth emails, so both say
> "1 hour". If you ever see a separate "Confirm signup" expiry, keep them consistent.

---

## Quick test checklist

- [ ] Resend domain shows **Verified** (Part A).
- [ ] `RESEND_FROM` updated in Vercel + redeployed (Part A).
- [ ] Sign out, then sign in with a **non-owner** email — the sign-in email arrives,
      from **PodcastAI**, with the warm copy (Parts B + C).
- [ ] Subscribe that account to a show; confirm a summary email arrives once a new
      episode is scanned (instant cadence) or via the daily digest.
