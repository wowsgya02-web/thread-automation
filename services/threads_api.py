"""Meta Threads Graph API로 본문 발행 (브라우저/Playwright 불필요)."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx

import config
from models import PublishResult

logger = logging.getLogger(__name__)

API_BASE = "https://graph.threads.net/v1.0"
MAX_TEXT_CHARS = 500
CONTAINER_WAIT_SECONDS = 45
CONTAINER_POLL_INTERVAL = 2.0


def credentials_path(username: str = "") -> Path:
    if username:
        from services.auth import user_dir

        return user_dir(username) / "threads_api.json"
    return config.STATE_DIR / "threads_api.json"


def load_credentials(username: str = "", path: Path | None = None) -> dict:
    file_path = path or credentials_path(username)
    if not file_path.is_file():
        # 전역 .env 폴백
        token = getattr(config, "THREADS_ACCESS_TOKEN", "") or ""
        user_id = getattr(config, "THREADS_USER_ID", "") or ""
        if token and user_id:
            return {"access_token": token, "user_id": user_id}
        return {}
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_credentials(
    access_token: str,
    user_id: str,
    *,
    username: str = "",
    threads_username: str = "",
    path: Path | None = None,
) -> Path:
    file_path = path or credentials_path(username)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "access_token": access_token.strip(),
        "user_id": str(user_id).strip(),
        "threads_username": (threads_username or "").strip(),
    }
    file_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return file_path


def clear_credentials(username: str = "") -> None:
    path = credentials_path(username)
    if path.is_file():
        path.unlink()


def credentials_ok(username: str = "") -> bool:
    data = load_credentials(username)
    return bool(data.get("access_token") and data.get("user_id"))


def _api_get(path: str, access_token: str, params: dict | None = None) -> dict:
    query = dict(params or {})
    query["access_token"] = access_token
    response = httpx.get(f"{API_BASE}/{path.lstrip('/')}", params=query, timeout=60.0)
    data = response.json()
    if response.status_code >= 400 or data.get("error"):
        raise RuntimeError(_format_error(data))
    return data


def _api_post(path: str, access_token: str, data: dict) -> dict:
    payload = dict(data)
    payload["access_token"] = access_token
    response = httpx.post(f"{API_BASE}/{path.lstrip('/')}", data=payload, timeout=60.0)
    body = response.json()
    if response.status_code >= 400 or body.get("error"):
        raise RuntimeError(_format_error(body))
    return body


def _format_error(data: dict) -> str:
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        message = err.get("message") or err.get("error_user_msg") or str(err)
        code = err.get("code")
        return f"Threads API 오류{f' ({code})' if code else ''}: {message}"
    return f"Threads API 오류: {data}"


def verify_token(access_token: str) -> dict:
    """토큰으로 프로필을 확인하고 user_id를 얻는다."""
    data = _api_get("me", access_token, {"fields": "id,username,name"})
    if not data.get("id"):
        raise RuntimeError("Threads 사용자 ID를 받지 못했습니다. 토큰 권한을 확인하세요.")
    return data


def truncate_for_threads(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    content = (text or "").strip()
    if len(content) <= limit:
        return content
    clipped = content[: max(0, limit - 1)].rstrip()
    return clipped + "…"


def _wait_container_ready(container_id: str, access_token: str) -> None:
    deadline = time.time() + CONTAINER_WAIT_SECONDS
    last_status = ""
    while time.time() < deadline:
        data = _api_get(container_id, access_token, {"fields": "status,error_message"})
        status = str(data.get("status") or "").upper()
        last_status = status
        if status in {"FINISHED", "PUBLISHED"}:
            return
        if status in {"ERROR", "EXPIRED"}:
            detail = data.get("error_message") or status
            raise RuntimeError(f"Threads 컨테이너 준비 실패: {detail}")
        time.sleep(CONTAINER_POLL_INTERVAL)
    raise TimeoutError(
        f"Threads 컨테이너가 준비되지 않았습니다 (마지막 상태: {last_status or 'unknown'})."
    )


def publish_via_api(
    content: str,
    comment_url: str | None = None,
    *,
    username: str = "",
    credentials: dict | None = None,
) -> PublishResult:
    creds = credentials or load_credentials(username)
    access_token = str(creds.get("access_token") or "").strip()
    user_id = str(creds.get("user_id") or "").strip()
    if not access_token or not user_id:
        return PublishResult(
            success=False,
            error=(
                "Threads API 토큰이 없습니다. 대시보드에서 Access Token을 연결하세요. "
                "(Meta 개발자 앱 → Threads → 토큰)"
            ),
        )

    text = truncate_for_threads(content)
    if not text:
        return PublishResult(success=False, error="발행할 본문이 비어 있습니다.")

    comment_url = (comment_url or config.SERVICE_URL or "").strip()
    try:
        created = _api_post(
            f"{user_id}/threads",
            access_token,
            {"media_type": "TEXT", "text": text},
        )
        creation_id = str(created.get("id") or "")
        if not creation_id:
            raise RuntimeError("creation_id를 받지 못했습니다.")
        _wait_container_ready(creation_id, access_token)
        published = _api_post(
            f"{user_id}/threads_publish",
            access_token,
            {"creation_id": creation_id},
        )
        media_id = str(published.get("id") or "")
        post_url = f"https://www.threads.net/post/{media_id}" if media_id else ""

        comment_ok = False
        comment_error = ""
        if comment_url and media_id:
            try:
                reply_text = truncate_for_threads(comment_url)
                reply = _api_post(
                    f"{user_id}/threads",
                    access_token,
                    {
                        "media_type": "TEXT",
                        "text": reply_text,
                        "reply_to_id": media_id,
                    },
                )
                reply_id = str(reply.get("id") or "")
                if reply_id:
                    _wait_container_ready(reply_id, access_token)
                    _api_post(
                        f"{user_id}/threads_publish",
                        access_token,
                        {"creation_id": reply_id},
                    )
                    comment_ok = True
            except Exception as exc:
                logger.exception("본문 발행 후 댓글(답글) 실패")
                comment_error = str(exc)

        return PublishResult(
            success=True,
            post_url=post_url,
            comment_ok=comment_ok if comment_url else True,
            error=comment_error,
        )
    except Exception as exc:
        logger.exception("Threads API 발행 실패")
        return PublishResult(success=False, error=str(exc))
