"""환경변수 및 프로젝트 경로 설정."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _from_streamlit_secrets(key: str) -> str:
    try:
        from streamlit import runtime

        if not runtime.exists():
            return ""
        import streamlit as st

        value = st.secrets.get(key)
        if value is None:
            return ""
        return str(value).strip()
    except Exception:
        return ""


def _env(key: str, default: str = "") -> str:
    secret = _from_streamlit_secrets(key)
    if secret:
        return secret
    value = os.getenv(key, default)
    return value.strip() if isinstance(value, str) else default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key, "true" if default else "false").lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _env_int(key: str, default: int) -> int:
    raw = _env(key, str(default))
    try:
        return int(raw)
    except ValueError:
        return default


def _resolve(path_value: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        path = ROOT / path
    return path


GEMINI_API_KEY = _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-3.6-flash")

TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")

SERVICE_URL = _env("SERVICE_URL")
SERVICE_NAME = _env("SERVICE_NAME", "MyService")

DOCS_DIR = _resolve(_env("DOCS_DIR", "docs"))
DATABASE_PATH = _resolve(_env("DATABASE_PATH", "database/history.db"))
SESSION_PATH = _resolve(_env("SESSION_PATH", "session/threads_session.json"))
THREADS_BASE_URL = _env("THREADS_BASE_URL", "https://www.threads.com").rstrip("/")

DEDUP_DAYS = _env_int("DEDUP_DAYS", 7)
LANGUAGE = _env("LANGUAGE", "ko").lower()

HEADLESS = _env_bool("HEADLESS", True)
PLAYWRIGHT_SLOW_MO = _env_int("PLAYWRIGHT_SLOW_MO", 0)

TIMEZONE = _env("TIMEZONE", "Asia/Seoul")
SCHEDULE_HOUR = _env_int("SCHEDULE_HOUR", 9)
SCHEDULE_MINUTE = _env_int("SCHEDULE_MINUTE", 0)
REVIEW_TIMEOUT_SECONDS = _env_int("REVIEW_TIMEOUT_SECONDS", 1800)

STATE_DIR = _resolve(_env("STATE_DIR", "state"))
SCHEDULE_FILE = _resolve(_env("SCHEDULE_FILE", "state/publish_schedule.json"))
ACCOUNTS_PATH = _resolve(_env("ACCOUNTS_PATH", "database/accounts.db"))

LOGS_DIR = ROOT / "logs"


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)


def configure_stdio() -> None:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def require_keys(*names: str) -> None:
    mapping = {
        "GEMINI_API_KEY": GEMINI_API_KEY,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "SERVICE_URL": SERVICE_URL,
    }
    missing = []
    for name in names:
        if not mapping.get(name):
            missing.append(name)
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            f"필수 환경변수가 없습니다: {joined}. .env.example을 참고해 .env를 작성하세요."
        )
