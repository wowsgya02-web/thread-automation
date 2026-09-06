"""대시보드 로그인 및 사용자별 데이터 경로."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import config

KST = timezone(timedelta(hours=9))
USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,32}$")
PBKDF2_ROUNDS = 210_000


@dataclass
class User:
    username: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def sanitize_username(username: str) -> str:
    name = (username or "").strip()
    if not USERNAME_RE.fullmatch(name):
        raise ValueError("아이디는 3~32자, 영문·숫자·._- 만 사용할 수 있습니다.")
    return name


def _accounts_path() -> Path:
    path = getattr(config, "ACCOUNTS_PATH", None) or (config.ROOT / "database" / "accounts.db")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_accounts_path())
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            role TEXT NOT NULL DEFAULT 'member',
            salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    return conn


def _hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ROUNDS,
    )
    return salt.hex(), digest.hex()


def user_dir(username: str) -> Path:
    safe = sanitize_username(username)
    path = config.STATE_DIR / "users" / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_schedule_file(username: str) -> Path:
    return user_dir(username) / "publish_schedule.json"


def user_docs_dir(username: str) -> Path:
    path = user_dir(username) / "docs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_session_file(username: str) -> Path:
    return user_dir(username) / "threads_session.json"


def ensure_member_docs(username: str) -> Path:
    directory = user_docs_dir(username)
    for name in ("service.md", "pain_points.md"):
        path = directory / name
        if not path.is_file():
            path.write_text("", encoding="utf-8")
    return directory


def user_settings_file(username: str) -> Path:
    return user_dir(username) / "settings.json"


def load_user_settings(username: str) -> dict:
    path = user_settings_file(username)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_user_settings(username: str, settings: dict) -> None:
    path = user_settings_file(username)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_user_settings(username)
    current.update(settings)
    path.write_text(
        json.dumps(current, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_launch_date(username: str) -> date:
    raw = str(load_user_settings(username).get("launch_date") or "")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return date.today()


def save_launch_date(username: str, value: date) -> None:
    save_user_settings(username, {"launch_date": value.isoformat()})


def list_users() -> list[User]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT username, role FROM users ORDER BY username"
        ).fetchall()
        return [User(username=row["username"], role=row["role"]) for row in rows]
    finally:
        conn.close()


def has_any_user() -> bool:
    return bool(list_users())


def create_user(username: str, password: str, role: str = "member") -> User:
    name = sanitize_username(username)
    if len(password or "") < 8:
        raise ValueError("비밀번호는 8자 이상이어야 합니다.")
    chosen_role = "admin" if role == "admin" else "member"
    salt, digest = _hash_password(password)
    now = datetime.now(KST).isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO users (username, role, salt, password_hash, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, chosen_role, salt, digest, now),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError("이미 있는 아이디입니다.") from exc
    finally:
        conn.close()
    return User(username=name, role=chosen_role)


def verify_user(username: str, password: str) -> User | None:
    name = (username or "").strip()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT username, role, salt, password_hash FROM users WHERE username = ?",
            (name,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    _, digest = _hash_password(password or "", row["salt"])
    if not hmac.compare_digest(digest, row["password_hash"]):
        return None
    return User(username=row["username"], role=row["role"])


def migrate_legacy_files(username: str) -> None:
    dest = user_schedule_file(username)
    if dest.is_file():
        return
    legacy = config.SCHEDULE_FILE
    if legacy.is_file():
        dest.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
