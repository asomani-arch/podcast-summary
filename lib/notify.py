"""Email delivery via Resend."""
import os
import resend
from markdown import markdown


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
      <hr style="border: none; border-top: 1px solid #eee; margin: 1.5rem 0 0.5rem;" />
      <p style="color: #aaa; font-size: 0.8rem;">Sent by your podcast-summary agent.</p>
    </div>
    """
    sender = os.getenv("RESEND_FROM", "Podcast Summary <onboarding@resend.dev>")
    result = _client().Emails.send({
        "from": sender,
        "to": [to_email],
        "subject": f"🎙️ {episode_title}",
        "html": body_html,
    })
    return result.get("id", "")
