"""텔레그램 인라인 버튼으로 초안 검토·승인·재생성·취소를 처리한다."""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Awaitable, Callable

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import config
from database.db_manager import get_db
from models import PostRecord, ServiceAnalysis

logger = logging.getLogger(__name__)

GenerateHook = Callable[..., Awaitable[int | None]]

_review_event: asyncio.Event | None = None


def arm_review_wait() -> asyncio.Event:
    global _review_event
    _review_event = asyncio.Event()
    return _review_event


def _finish_review() -> None:
    if _review_event is not None and not _review_event.is_set():
        _review_event.set()


def build_application(on_generate: GenerateHook | None = None) -> Application:
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN이 필요합니다.")

    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", _cmd_start))
    application.add_handler(CommandHandler("status", _cmd_status))
    if on_generate is not None:
        application.add_handler(
            CommandHandler("generate", _make_generate_handler(on_generate))
        )
    application.add_handler(CallbackQueryHandler(_on_callback))
    return application


def review_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("발행 승인", callback_data=f"approve:{post_id}"),
                InlineKeyboardButton("재생성", callback_data=f"regen:{post_id}"),
                InlineKeyboardButton("취소", callback_data=f"cancel:{post_id}"),
            ]
        ]
    )


def format_review(
    post: PostRecord,
    persona: str = "",
    analysis: ServiceAnalysis | None = None,
) -> str:
    header = f"<b>스레드 초안 #{post.id}</b>"
    meta_lines = [
        f"주제: {html.escape(post.topic)}",
        f"소구점: {html.escape(post.pain_point)}",
    ]
    if persona:
        meta_lines.append(f"페르소나: {html.escape(persona)}")
    if analysis:
        sources = ", ".join(analysis.source_files) or "-"
        meta_lines.append(f"문서: {html.escape(sources)}")
        if analysis.used_fallback:
            meta_lines.append("docs가 비어 샘플 템플릿으로 생성됨")

    body = html.escape(post.content)
    return (
        f"{header}\n"
        + "\n".join(meta_lines)
        + "\n\n────────────\n"
        + f"{body}\n"
        + "────────────\n\n"
        "승인하면 본문을 발행하고 10초 뒤 첫 댓글로 서비스 링크를 답니다."
    )


def telegram_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def _review_markup(post_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "발행 승인", "callback_data": f"approve:{post_id}"},
                {"text": "재생성", "callback_data": f"regen:{post_id}"},
                {"text": "취소", "callback_data": f"cancel:{post_id}"},
            ]
        ]
    }


def send_telegram_message(
    text: str,
    *,
    reply_markup: dict | None = None,
    parse_mode: str | None = "HTML",
) -> None:
    if not telegram_configured():
        raise RuntimeError(
            "텔레그램 알림을 보낼 수 없습니다. .env의 TELEGRAM_BOT_TOKEN과 TELEGRAM_CHAT_ID를 확인하세요."
        )
    payload: dict = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": (text or "")[:4000],
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = httpx.post(url, json=payload, timeout=30.0)
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"텔레그램 전송에 실패했습니다: {exc}") from exc
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or "텔레그램 전송에 실패했습니다.")


def send_for_review_sync(
    post: PostRecord,
    persona: str = "",
    analysis: ServiceAnalysis | None = None,
) -> None:
    send_telegram_message(
        format_review(post, persona=persona, analysis=analysis),
        reply_markup=_review_markup(post.id),
    )
    logger.info("텔레그램 검토 메시지 전송 (post_id=%s)", post.id)


async def send_for_review(
    bot,
    post: PostRecord,
    persona: str = "",
    analysis: ServiceAnalysis | None = None,
) -> None:
    await asyncio.to_thread(send_for_review_sync, post, persona, analysis)


async def notify_plain(bot=None, text: str = "") -> None:
    if not (text or "").strip():
        return
    if not telegram_configured():
        logger.warning("텔레그램이 설정되지 않아 알림을 건너뜁니다.")
        return
    try:
        await asyncio.to_thread(
            send_telegram_message, text, reply_markup=None, parse_mode=None
        )
    except Exception:
        logger.exception("텔레그램 알림 전송 실패")


def _authorized(update: Update) -> bool:
    allowed = str(config.TELEGRAM_CHAT_ID)
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    user_id = str(update.effective_user.id) if update.effective_user else ""
    return allowed in {chat_id, user_id}


async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _authorized(update):
        return
    await update.message.reply_text(
        "SNS 마케팅 반자동화 봇입니다.\n"
        "/generate — 초안 1건 생성\n"
        "/status — 최근 기록\n\n"
        "초안이 오면 [발행 승인] / [재생성] / [취소]로 처리하세요."
    )


async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _authorized(update):
        return
    db = get_db()
    topics = db.get_recent_topics(10)
    if not topics:
        await update.message.reply_text("아직 기록이 없습니다.")
        return
    lines = ["최근 주제 10개:"] + [f"• {topic}" for topic in topics]
    await update.message.reply_text("\n".join(lines))


def _make_generate_handler(on_generate: GenerateHook):
    async def _cmd_generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not _authorized(update):
            return
        await update.message.reply_text("초안을 생성하고 있습니다…")
        try:
            await on_generate(context.bot)
        except Exception as exc:
            logger.exception("수동 생성 실패")
            await update.message.reply_text(f"생성 실패: {exc}")

    return _cmd_generate


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not _authorized(update):
        if query:
            await query.answer("허용되지 않은 계정입니다.", show_alert=True)
        return

    data = query.data or ""
    if ":" not in data:
        await query.answer()
        return

    action, _, raw_id = data.partition(":")
    try:
        post_id = int(raw_id)
    except ValueError:
        await query.answer("잘못된 요청입니다.", show_alert=True)
        return

    db = get_db()
    record = db.get_post(post_id)
    if record is None:
        await query.answer("해당 초안을 찾을 수 없습니다.", show_alert=True)
        return

    if action == "approve":
        await _handle_approve(query, context, record)
    elif action == "regen":
        await _handle_regen(query, record)
    elif action == "cancel":
        await _handle_cancel(query, record)
    else:
        await query.answer()


async def _handle_approve(query, context: ContextTypes.DEFAULT_TYPE, record: PostRecord) -> None:
    from services.threads_publisher import publish_thread

    if record.status == "posted":
        await query.answer("이미 발행된 초안입니다.", show_alert=True)
        return

    await query.answer("발행을 시작합니다")
    await _safe_edit(query, context, "⏳ Threads에 발행하는 중입니다. 잠시만 기다려 주세요.")

    db = get_db()
    db.update_status(record.id, "approved")

    result = await asyncio.to_thread(publish_thread, record.content, config.SERVICE_URL)
    if result.success:
        db.mark_posted(record.id)
        lines = ["✅ 발행이 완료되었습니다."]
        if result.post_url:
            lines.append(result.post_url)
        if result.comment_ok:
            lines.append("💬 첫 댓글로 서비스 링크를 등록했습니다.")
        else:
            lines.append("⚠️ 본문은 올라갔지만 댓글 등록은 실패했습니다. 수동으로 링크를 달아 주세요.")
            if result.error:
                lines.append(result.error)
        await _safe_edit(query, context, "\n".join(lines))
    else:
        db.update_status(record.id, "failed")
        detail = result.error or "알 수 없는 오류"
        extra = f"\n스크린샷: {result.screenshot_path}" if result.screenshot_path else ""
        await _safe_edit(
            query,
            context,
            f"발행 실패\n{detail}{extra}\n\n토큰이 만료됐다면 대시보드에서 Threads API 토큰을 다시 연결하세요.",
        )
    _finish_review()


async def _handle_regen(query, record: PostRecord) -> None:
    from pipeline import build_draft

    await query.answer("새 초안을 만드는 중")
    await query.edit_message_text("🔄 다른 각도로 재생성하고 있습니다…")
    try:
        db = get_db()
        analysis, draft = build_draft(db, extra_exclude=[record.topic, record.pain_point])
        db.update_content(record.id, draft, status="pending")
        updated = db.get_post(record.id)
        assert updated is not None
        await query.edit_message_text(
            format_review(updated, persona=draft.persona, analysis=analysis),
            parse_mode="HTML",
            reply_markup=review_keyboard(record.id),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception("재생성 실패")
        await query.edit_message_text(
            f"재생성에 실패했습니다: {exc}\n이전 초안은 DB에 남아 있습니다.",
            reply_markup=review_keyboard(record.id),
        )


async def _handle_cancel(query, record: PostRecord) -> None:
    await query.answer("취소했습니다")
    get_db().update_status(record.id, "cancelled")
    await query.edit_message_text(f"🚫 초안 #{record.id}을(를) 취소했습니다.")
    _finish_review()


async def _safe_edit(query, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    try:
        await query.edit_message_text(text[:4000], disable_web_page_preview=True)
    except TelegramError:
        await context.bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=text[:4000],
            disable_web_page_preview=True,
        )
