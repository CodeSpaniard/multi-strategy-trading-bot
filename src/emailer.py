"""SMTP email transport for daily summaries.

Failures are logged at WARNING and swallowed — daily summaries are
informational, so SMTP problems should not cascade into runtime
errors or Pushover noise. The file write in daily_summary.py is the
durable record; email is the delivery layer.

Reads from env:
  SMTP_HOST           (default: smtp.gmail.com)
  SMTP_PORT           (default: 587)
  SMTP_USERNAME       (required)
  SMTP_APP_PASSWORD   (required; Gmail App Password — not regular password)
  SUMMARY_EMAIL_TO    (default for to_address if not provided)
  SUMMARY_EMAIL_FROM  (default for from_address; falls back to SMTP_USERNAME)
"""
import logging
import os
import smtplib
from email.mime.text import MIMEText
from typing import Optional

log = logging.getLogger(__name__)

_DEFAULT_SMTP_HOST = "smtp.gmail.com"
_DEFAULT_SMTP_PORT = 587
_SMTP_TIMEOUT_S = 15


def send_email(
    subject: str,
    body: str,
    to_address: Optional[str] = None,
    from_address: Optional[str] = None,
) -> bool:
    """Send a plaintext email via SMTP+STARTTLS.

    Returns True on success, False on any failure. Never raises.
    """
    smtp_host = os.environ.get("SMTP_HOST", _DEFAULT_SMTP_HOST)
    smtp_port = int(os.environ.get("SMTP_PORT", _DEFAULT_SMTP_PORT))
    smtp_user = os.environ.get("SMTP_USERNAME")
    smtp_pass = os.environ.get("SMTP_APP_PASSWORD")

    to_addr = to_address or os.environ.get("SUMMARY_EMAIL_TO")
    from_addr = from_address or os.environ.get("SUMMARY_EMAIL_FROM") or smtp_user

    if not smtp_user or not smtp_pass:
        log.warning("send_email: SMTP_USERNAME or SMTP_APP_PASSWORD not set; skipping")
        return False

    if not to_addr:
        log.warning("send_email: no recipient (SUMMARY_EMAIL_TO not set and to_address not provided); skipping")
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=_SMTP_TIMEOUT_S) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        return True
    except Exception as e:
        log.warning(f"send_email failed: {e}")
        return False
