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


class DailyRotatingFileHandler(logging.FileHandler):
    """FileHandler that reopens at date change, preserving YYYY-MM-DD.{name}.log naming.

    Bug fix for long-running bots: the prior FileHandler captured the date once at
    boot, so all logs accumulated in the start-date file regardless of how many
    midnights passed. This handler re-checks the date on each emit and reopens
    the stream when it changes.
    """

    def __init__(self, dir_path: str, bot_name: str):
        self._dir = dir_path
        self._bot = bot_name
        self._current_date = datetime.now().strftime("%Y-%m-%d")
        super().__init__(self._compute_path(), mode="a", delay=False)

    def _compute_path(self) -> str:
        return f"{self._dir}/{self._current_date}.{self._bot}.log"

    def emit(self, record):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._current_date:
            self._current_date = today
            self.baseFilename = self._compute_path()
            if self.stream:
                self.stream.close()
                self.stream = self._open()
        super().emit(record)


def get_logger(name: str = "bot") -> logging.Logger:
    ensure_dirs()

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = DailyRotatingFileHandler(DAILY_DIR, name)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger
