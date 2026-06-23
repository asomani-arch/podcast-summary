"""Email delivery via Resend."""
import os
import resend
from markdown import markdown

BRAND = "PodcastAI"
DEFAULT_SENDER = f"{BRAND} <onboarding@resend.dev>"

# Shared email footer: brand, gentle privacy reassurance, and copyright. Matches
# the on-site fine print so the email doesn't read as spam.
_FOOTER = (
    '<hr style="border:none;border-top:1px solid #eee;margin:1.5rem 0 0.75rem;" />'
    '<p style="color:#aaa;font-size:0.78rem;line-height:1.5;margin:0;">'
    f'Sent by {BRAND}. You\'re receiving this because you subscribed to podcast '
    'summaries. We never sell your data or send spam.<br>'
    '© 2026 Ashutosh Somani'
    '</p>'
)


def _client():
    resend.api_key = os.environ["RESEND_API_KEY"]
    return resend


def send_summary_email(
    to_email: str,
    podcast_title: str,
    episode_title: str,
    summary_markdown: str,
) -> str:
    """Send a summary email. Returns the Resend message ID."""
    body_html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 640px; margin: 0 auto; color: #222;">
      <h1 style="font-size: 1.3rem; color: #1a1a2e; margin-bottom: 0.25rem;">{episode_title}</h1>
      <p style="color: #888; margin-top: 0;">{podcast_title}</p>
      <hr style="border: none; border-top: 1px solid #eee; margin: 1rem 0;" />
      {markdown(summary_markdown)}
      {_FOOTER}
    </div>
    """
    sender = os.getenv("RESEND_FROM", DEFAULT_SENDER)
    result = _client().Emails.send({
        "from": sender,
        "to": [to_email],
        "subject": f"🎙️ {episode_title}",
        "html": body_html,
    })
    return result.get("id", "")


def send_digest_email(to_email: str, items: list[dict]) -> str:
    """Send one batched digest email covering several episode summaries.
    `items` = [{podcast_title, episode_title, summary_md}, ...]."""
    blocks = []
    for it in items:
        blocks.append(
            f'<h2 style="font-size:1.15rem;color:#1a1a2e;margin:1.75rem 0 0.1rem;">{it.get("episode_title","")}</h2>'
            f'<p style="color:#888;margin:0 0 0.5rem;font-size:0.85rem;">{it.get("podcast_title","")}</p>'
            f'{markdown(it.get("summary_md") or "")}'
            '<hr style="border:none;border-top:1px solid #eee;margin:1.5rem 0 0;" />'
        )
    body_html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 640px; margin: 0 auto; color: #222;">
      <h1 style="font-size: 1.3rem; color: #1a1a2e;">Your {BRAND} digest</h1>
      <p style="color: #888; margin-top: 0;">{len(items)} new episode{'s' if len(items) != 1 else ''} summarized.</p>
      <hr style="border: none; border-top: 1px solid #eee; margin: 1rem 0;" />
      {''.join(blocks)}
      {_FOOTER}
    </div>
    """
    sender = os.getenv("RESEND_FROM", DEFAULT_SENDER)
    result = _client().Emails.send({
        "from": sender,
        "to": [to_email],
        "subject": f"🎙️ Your podcast digest — {len(items)} new episode{'s' if len(items) != 1 else ''}",
        "html": body_html,
    })
    return result.get("id", "")
