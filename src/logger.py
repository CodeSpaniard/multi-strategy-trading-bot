import logging
import os
from datetime import datetime


# Shared log layout constants — single source of truth for all writers/readers.
LOGS_ROOT = "logs"
STATE_DIR = f"{LOGS_ROOT}/state"
DAILY_DIR = f"{LOGS_ROOT}/daily"
MANUAL_DIR = f"{LOGS_ROOT}/manual"
ARCHIVE_DIR = f"{LOGS_ROOT}/archive"
SUMMARY_LOG = f"{LOGS_ROOT}/summary.log"


def ensure_dirs() -> None:
    for d in (LOGS_ROOT, STATE_DIR, DAILY_DIR, MANUAL_DIR, ARCHIVE_DIR):
        os.makedirs(d, exist_ok=True)


def state_path(bot_name: str) -> str:
    return f"{STATE_DIR}/{bot_name}.state.json"


def pid_path(bot_name: str) -> str:
    return f"{STATE_DIR}/{bot_name}.pid"


def get_logger(name: str = "bot") -> logging.Logger:
    ensure_dirs()
    log_path = f"{DAILY_DIR}/{datetime.now().strftime('%Y-%m-%d')}.{name}.log"

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_path)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger
