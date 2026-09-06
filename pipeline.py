"""문서 분석 → 본문 생성 → 중복 필터까지 한 사이클."""

from __future__ import annotations

import asyncio
import logging

import config
from database.db_manager import DBManager, get_db
from models import ContentDraft, ServiceAnalysis
from services.content_generator import generate_post
from services.doc_analyzer import analyze_documents
from services.schedule_config import load_schedule

logger = logging.getLogger(__name__)


def _user_context(username: str = "") -> tuple[object | None, bool, object | None]:
    if not username:
        return None, False, None
    from services.auth import list_users, user_docs_dir, user_session_file

    user = next((item for item in list_users() if item.username == username), None)
    session = user_session_file(username)
    if user is not None and not user.is_admin:
        return user_docs_dir(username), True, session
    return None, False, session


def build_draft(
    db: DBManager | None = None,
    extra_exclude: list[str] | None = None,
    username: str = "",
) -> tuple[ServiceAnalysis, ContentDraft]:
    db = db or get_db()
    docs_dir, use_generic, _session = _user_context(username)
    analysis = analyze_documents(
        docs_dir=docs_dir,
        use_generic_strategy=use_generic,
    )
    recent_topics = db.get_recent_topics(10)
    blocked = db.get_blocked_terms(config.DEDUP_DAYS)
    if extra_exclude:
        blocked = blocked + extra_exclude

    hint = _angle_hint(analysis, blocked)
    phase = "W-4: 고객 문제 정의 & 공감대 형성 (Awareness)"
    draft = generate_post(
        analysis,
        recent_topics=recent_topics,
        blocked_terms=blocked,
        extra_instructions=hint,
        phase=phase,
    )
    if db.is_duplicate(draft.topic, draft.pain_point, config.DEDUP_DAYS):
        logger.info("DB 중복 감지, 다른 각도로 재시도합니다: %s", draft.topic)
        blocked = blocked + [draft.topic, draft.pain_point]
        draft = generate_post(
            analysis,
            recent_topics=recent_topics,
            blocked_terms=blocked,
            extra_instructions=hint + " 방금 고른 주제는 이미 사용됨. 완전히 다른 일상 장면으로.",
            phase=phase,
        )
        if db.is_duplicate(draft.topic, draft.pain_point, config.DEDUP_DAYS):
            raise RuntimeError(
                f"최근 {config.DEDUP_DAYS}일 내 주제와 겹치지 않는 초안을 만들지 못했습니다."
            )
    return analysis, draft


async def generate_and_notify(bot=None, username: str = "") -> int:
    from services.telegram_reviewer import send_for_review

    db = get_db()
    analysis, draft = build_draft(db, username=username)
    post_id = db.insert_post(draft, status="pending")
    record = db.get_post(post_id)
    if record is None:
        raise RuntimeError("초안 저장 후 레코드를 읽지 못했습니다.")
    await send_for_review(bot, record, persona=draft.persona, analysis=analysis)
    logger.info("파이프라인 1회 완료, 검토 대기 중 (post_id=%s user=%s)", post_id, username or "-")
    return post_id


async def generate_and_auto_publish(bot=None, username: str = "") -> int:
    from services.telegram_reviewer import notify_plain
    from services.threads_publisher import publish_thread

    db = get_db()
    _docs_dir, _use_generic, session_path = _user_context(username)
    _, draft = build_draft(db, username=username)
    post_id = db.insert_post(draft, status="approved")
    logger.info("자동 발행 시작 (post_id=%s topic=%s user=%s)", post_id, draft.topic, username or "-")

    result = await asyncio.to_thread(
        publish_thread,
        draft.content,
        config.SERVICE_URL,
        session_path,
        username,
    )
    if result.success:
        db.mark_posted(post_id)
        lines = ["스케줄 자동 발행이 완료되었습니다.", f"주제: {draft.topic}"]
        if result.post_url:
            lines.append(result.post_url)
        if result.comment_ok:
            lines.append("첫 댓글로 서비스 링크를 등록했습니다.")
        else:
            lines.append("본문은 올라갔지만 댓글 등록은 실패했습니다. 수동으로 링크를 달아 주세요.")
            if result.error:
                lines.append(result.error)
        logger.info("자동 발행 성공 (post_id=%s)", post_id)
    else:
        db.update_status(post_id, "failed")
        detail = result.error or "알 수 없는 오류"
        extra = f"\n스크린샷: {result.screenshot_path}" if result.screenshot_path else ""
        lines = [
            "스케줄 자동 발행에 실패했습니다.",
            f"주제: {draft.topic}",
            detail + extra,
            "토큰이 만료됐다면 대시보드에서 Threads API 토큰을 다시 연결하세요.",
        ]
        logger.error("자동 발행 실패 (post_id=%s): %s", post_id, detail)

    await notify_plain(bot, "\n".join(lines))
    return post_id


async def run_scheduled_job(bot=None, username: str = "") -> int | None:
    from services.auth import user_schedule_file

    path = user_schedule_file(username) if username else None
    schedule = load_schedule(path)
    if not schedule.enabled:
        logger.info("스케줄이 꺼져 있어 건너뜁니다. (user=%s)", username or "-")
        return None
    if schedule.mode == "auto":
        return await generate_and_auto_publish(bot, username=username)
    return await generate_and_notify(bot, username=username)


def _angle_hint(analysis: ServiceAnalysis, blocked: list[str]) -> str:
    blocked_l = {term.lower() for term in blocked}
    unused = [
        point.keyword
        for point in analysis.pain_points
        if point.keyword.lower() not in blocked_l
        and not any(point.keyword.lower() in term for term in blocked_l)
    ]
    if unused:
        joined = ", ".join(unused[:8])
        return (
            f"고충 인벤토리에서 아직 안 쓴 키워드 중 딱 1개만 고르세요: {joined}"
        )
    return "최근 고충을 이미 썼다면 같은 키워드라도 다른 장면(요일/체급/동행)으로만 바꾸세요."
