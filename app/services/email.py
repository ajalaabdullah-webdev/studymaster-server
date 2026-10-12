"""Transactional email via Resend.

Non-blocking-ish: failures are logged, never raised into the request path, so a
mail hiccup never blocks signup/verification. In development (no key) emails are
logged to the console instead of sent.
"""

import logging

import resend

from app.config import settings

logger = logging.getLogger("studymaster.email")

if settings.resend_api_key:
    resend.api_key = settings.resend_api_key


def _send(to: str, subject: str, html: str) -> None:
    if not settings.resend_api_key:
        logger.info("[email:dev] to=%s subject=%s\n%s", to, subject, html)
        return
    try:
        resend.Emails.send(
            {"from": settings.email_from, "to": [to], "subject": subject, "html": html}
        )
    except Exception as exc:  # noqa: BLE001 - email must never break the request
        logger.error("Failed to send email to %s: %s", to, exc)


def send_otp_email(to: str, username: str, otp: str) -> None:
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:480px;margin:auto">
      <h2>Welcome to StudyMaster, {username}!</h2>
      <p>Your verification code is:</p>
      <p style="font-size:32px;font-weight:700;letter-spacing:6px">{otp}</p>
      <p>This code expires in 10 minutes.</p>
    </div>
    """
    _send(to, "Verify your StudyMaster account", html)


def send_reset_email(to: str, username: str, reset_url: str) -> None:
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:480px;margin:auto">
      <h2>Reset your StudyMaster password</h2>
      <p>Hi {username}, click below to reset your password:</p>
      <p><a href="{reset_url}"
        style="background:#2563eb;color:#fff;padding:12px 20px;border-radius:10px;
        text-decoration:none;display:inline-block">Reset password</a></p>
      <p>If you didn't request this, you can ignore this email.</p>
    </div>
    """
    _send(to, "Reset your StudyMaster password", html)
