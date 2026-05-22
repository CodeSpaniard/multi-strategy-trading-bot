import logging
import os
import urllib.request

log = logging.getLogger(__name__)


def ping_healthcheck() -> None:
    url = os.getenv("HEALTHCHECKS_PING_URL")
    if not url:
        return

    try:
        urllib.request.urlopen(url, timeout=5).read()
    except Exception as e:
        log.debug(f"healthcheck ping failed (non-fatal): {e}")
