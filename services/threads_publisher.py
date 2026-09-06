"""Threads 발행 진입점. Meta Graph API를 사용한다 (브라우저 불필요)."""

from __future__ import annotations

import logging
from pathlib import Path

from models import PublishResult
from services.threads_api import load_credentials, publish_via_api

logger = logging.getLogger(__name__)


def resolve_session_path(session_path: Path | None = None) -> Path:
    """하위 호환용. API 모드에서는 사용하지 않는다."""
    raise FileNotFoundError(
        "브라우저 세션 방식은 더 이상 사용하지 않습니다. "
        "대시보드에서 Threads API 토큰을 연결하세요."
    )


def publish_thread(
    content: str,
    comment_url: str | None = None,
    session_path: Path | None = None,
    username: str = "",
) -> PublishResult:
    """Threads Graph API로 발행. username 또는 전역/파일 토큰을 쓴다."""
    credentials = None
    if session_path is not None:
        # 예전 호출이 session_path에 사용자 폴더를 넘긴 경우 API 파일로 해석
        api_candidate = session_path.parent / "threads_api.json"
        if api_candidate.is_file():
            credentials = load_credentials(path=api_candidate)
        elif not username and session_path.parent.name:
            username = session_path.parent.name

    logger.info("Threads API 발행 시작 (user=%s)", username or "-")
    return publish_via_api(
        content,
        comment_url,
        username=username,
        credentials=credentials,
    )
