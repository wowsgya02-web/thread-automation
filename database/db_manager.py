"""SQLite 연동, 중복 방지, 발행 기록 관리."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterator

from models import ContentDraft, PostRecord

KST = timezone(timedelta(hours=9))
SIMILARITY_THRESHOLD = 0.72
ACTIVE_STATUSES = ("pending", "approved", "posted")


def _now() -> datetime:
    return datetime.now(KST)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=KST)
        return parsed
    except ValueError:
        return None


def _normalize(text: str) -> str:
    return "".join(text.lower().split())


def _similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= SIMILARITY_THRESHOLD


class DBManager:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    pain_point TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    posted_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status)"
            )

    def insert_post(self, draft: ContentDraft, status: str = "pending") -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO posts (topic, pain_point, content, status, created_at, posted_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (draft.topic, draft.pain_point, draft.content, status, _now_iso()),
            )
            return int(cursor.lastrowid)

    def update_content(self, post_id: int, draft: ContentDraft, status: str = "pending") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE posts
                SET topic = ?, pain_point = ?, content = ?, status = ?, created_at = ?
                WHERE id = ?
                """,
                (draft.topic, draft.pain_point, draft.content, status, _now_iso(), post_id),
            )

    def update_status(
        self,
        post_id: int,
        status: str,
        posted_at: str | None = None,
    ) -> None:
        if status == "posted" and posted_at is None:
            posted_at = _now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE posts
                SET status = ?, posted_at = COALESCE(?, posted_at)
                WHERE id = ?
                """,
                (status, posted_at, post_id),
            )

    def mark_posted(self, post_id: int) -> None:
        self.update_status(post_id, "posted", _now_iso())

    def get_post(self, post_id: int) -> PostRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def get_recent_topics(self, limit: int = 10) -> list[str]:
        """최근 발행(또는 대기)된 주제 목록."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT topic FROM posts
                WHERE status IN ('pending', 'approved', 'posted')
                ORDER BY COALESCE(posted_at, created_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        topics: list[str] = []
        seen: set[str] = set()
        for row in rows:
            topic = (row["topic"] or "").strip()
            key = _normalize(topic)
            if topic and key not in seen:
                seen.add(key)
                topics.append(topic)
        return topics

    def get_recent_pain_points(self, days: int) -> list[str]:
        cutoff = _now() - timedelta(days=days)
        points: list[str] = []
        for record in self._recent_active(days):
            created = _parse_dt(record.created_at)
            posted = _parse_dt(record.posted_at)
            stamp = posted or created
            if stamp and stamp >= cutoff and record.pain_point:
                points.append(record.pain_point)
        return points

    def get_blocked_terms(self, days: int) -> list[str]:
        """최근 N일간 다룬 주제/소구점 (중복 생성 차단용)."""
        terms: list[str] = []
        seen: set[str] = set()
        for record in self._recent_active(days):
            for value in (record.topic, record.pain_point):
                key = _normalize(value)
                if value and key not in seen:
                    seen.add(key)
                    terms.append(value)
        return terms

    def is_duplicate(self, topic: str, pain_point: str, days: int) -> bool:
        for record in self._recent_active(days):
            if _similar(topic, record.topic) or _similar(pain_point, record.pain_point):
                return True
        return False

    def _recent_active(self, days: int) -> list[PostRecord]:
        cutoff = (_now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM posts
                WHERE status IN ('pending', 'approved', 'posted')
                  AND COALESCE(posted_at, created_at) >= ?
                ORDER BY id DESC
                """,
                (cutoff,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> PostRecord:
        return PostRecord(
            id=int(row["id"]),
            topic=row["topic"],
            pain_point=row["pain_point"],
            content=row["content"],
            status=row["status"],
            created_at=row["created_at"],
            posted_at=row["posted_at"],
        )


_db: DBManager | None = None


def get_db(db_path: str | Path | None = None) -> DBManager:
    global _db
    if _db is None:
        from config import DATABASE_PATH

        _db = DBManager(db_path or DATABASE_PATH)
    return _db
