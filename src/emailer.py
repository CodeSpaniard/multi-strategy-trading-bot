"""HTTPS email transport for daily summaries via Resend.

Resend is used instead of SMTP because DigitalOcean blocks outbound
SMTP ports (25, 465, 587) on droplets as an anti-spam measure.
Resend's HTTPS API is not subject to that block.

Failures are logged at WARNING and swallowed — daily summaries are
informational, so transport problems should not cascade into
runtime errors or Pushover noise. The file write in daily_summary.py
is the durable record; email is the delivery layer.

Reads from env:
  RESEND_API_KEY      (required; starts with re_)
  SUMMARY_EMAIL_TO    (default for to_address if not provided)
  SUMMARY_EMAIL_FROM  (default for from_address; falls back to
                       onboarding@resend.dev)
"""
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

_RESEND_ENDPOINT = "https://api.resend.com/emails"
_HTTP_TIMEOUT_S = 15
_DEFAULT_FROM = "onboarding@resend.dev"


def send_email(
    subject: str,
    body: str,
    to_address: Optional[str] = None,
    from_address: Optional[str] = None,
) -> bool:
    """Send a plaintext email via the Resend HTTPS API.

    Returns True on success, False on any failure. Never raises.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    to_addr = to_address or os.environ.get("SUMMARY_EMAIL_TO")
    from_addr = from_address or os.environ.get("SUMMARY_EMAIL_FROM") or _DEFAULT_FROM

    if not api_key:
        log.warning("send_email: RESEND_API_KEY not set; skipping")
        return False

    if not to_addr:
        log.warning("send_email: no recipient (SUMMARY_EMAIL_TO not set and to_address not provided); skipping")
        return False

    payload = json.dumps({
        "from": from_addr,
        "to": [to_addr],
        "subject": subject,
        "text": body,
    }).encode("utf-8")

    req = urllib.request.Request(
        _RESEND_ENDPOINT,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            if 200 <= resp.status < 300:
                return True
            log.warning(f"send_email: unexpected status {resp.status}")
            return False
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            err_body = "<no body>"
        log.warning(f"send_email failed: HTTP {e.code}: {err_body}")
        return False
    except Exception as e:
        log.warning(f"send_email failed: {e}")
        return False
