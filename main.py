"""SNS 마케팅 반자동화 파이프라인 진입점.

사용법:
  python main.py once        초안 1건 생성 → 텔레그램 검토 → 승인 시 발행 후 종료
  python main.py schedule    대시보드 스케줄로 생성/발행 + 텔레그램 봇 상시 대기
  python main.py bot         텔레그램 봇만 실행 (/generate 로 수동 트리거)
  python main.py login [--user 아이디]  브라우저에서 Threads 로그인 후 세션 저장
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from zoneinfo import ZoneInfo

import config
from pipeline import generate_and_notify, run_scheduled_job
from services.schedule_config import MODE_LABELS, collect_user_schedules, schedules_signature
from services.telegram_reviewer import arm_review_wait, build_application

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Threads 마케팅 반자동화 파이프라인")
    parser.add_argument(
        "mode",
        nargs="?",
        default="once",
        choices=("once", "schedule", "bot", "login"),
        help="실행 모드 (기본: once)",
    )
    parser.add_argument(
        "--user",
        default="",
        help="login 모드에서 대시보드 사용자별 Threads 세션 경로를 씁니다.",
    )
    return parser.parse_args()


async def run_once() -> None:
    config.require_keys("GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    application = build_application(on_generate=generate_and_notify)
    done = arm_review_wait()

    await application.initialize()
    await application.start()
    assert application.updater is not None
    await application.updater.start_polling()
    try:
        await generate_and_notify(application.bot)
        timeout = config.REVIEW_TIMEOUT_SECONDS or None
        logger.info("텔레그램 승인 대기 중 (timeout=%s)", timeout or "무제한")
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("승인 대기 시간이 지나 프로세스를 종료합니다.")
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


def _apply_publish_jobs(scheduler, bot) -> str:
    from apscheduler.triggers.cron import CronTrigger

    for job in scheduler.get_jobs():
        if job.id.startswith("threads_slot_"):
            job.remove()

    registered = 0
    for username, schedule in collect_user_schedules():
        if not schedule.enabled:
            logger.info("자동 발행 스케줄이 꺼져 있습니다. (user=%s)", username or "default")
            continue
        timezone = ZoneInfo(schedule.timezone)
        label = username or "default"
        for stamp in schedule.times:
            hour_s, _, minute_s = stamp.partition(":")
            hour = int(hour_s)
            minute = int(minute_s or "0")
            job_id = f"threads_slot_{label}_{hour:02d}_{minute:02d}"

            async def _scheduled(user: str = username) -> None:
                logger.info("스케줄 트리거 (user=%s)", user or "default")
                try:
                    await run_scheduled_job(bot, username=user)
                except Exception:
                    logger.exception("스케줄 실행 실패 (user=%s)", user or "default")

            scheduler.add_job(
                _scheduled,
                CronTrigger(
                    day_of_week=schedule.cron_day_of_week(),
                    hour=hour,
                    minute=minute,
                    timezone=timezone,
                ),
                id=job_id,
                replace_existing=True,
            )
            registered += 1
        logger.info(
            "스케줄 등록 (user=%s): %s %s (%s, %s)",
            label,
            schedule.weekday_labels(),
            ", ".join(schedule.times),
            schedule.timezone,
            MODE_LABELS.get(schedule.mode, schedule.mode),
        )
    if registered == 0:
        logger.info("켜진 스케줄이 없습니다. 대시보드에서 켜면 20초 안에 반영됩니다.")
    return schedules_signature()


async def run_bot(with_schedule: bool) -> None:
    config.require_keys("GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    application = build_application(on_generate=generate_and_notify)

    scheduler = None
    if with_schedule:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        scheduler = AsyncIOScheduler(timezone=ZoneInfo(config.TIMEZONE))
        last_signature = _apply_publish_jobs(scheduler, application.bot)

        async def _reload_if_changed() -> None:
            nonlocal last_signature
            current = schedules_signature()
            if current == last_signature:
                return
            logger.info("스케줄 파일이 바뀌어 작업을 다시 등록합니다.")
            last_signature = _apply_publish_jobs(scheduler, application.bot)

        scheduler.add_job(
            _reload_if_changed,
            "interval",
            seconds=20,
            id="threads_schedule_reload",
            replace_existing=True,
        )
        scheduler.start()

    logger.info("텔레그램 봇 폴링 시작. /generate 로 수동 실행할 수 있습니다.")
    try:
        async with application:
            await application.start()
            assert application.updater is not None
            await application.updater.start_polling()
            await asyncio.Event().wait()
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


def run_login(username: str = "") -> None:
    from save_session import save_threads_session

    session_path = None
    if username:
        from services.auth import user_session_file

        session_path = user_session_file(username)
    save_threads_session(session_path)


def main() -> None:
    config.configure_stdio()
    config.setup_logging()
    args = parse_args()

    if args.mode == "login":
        run_login(args.user)
        return

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    if args.mode == "once":
        asyncio.run(run_once())
    elif args.mode == "schedule":
        asyncio.run(run_bot(with_schedule=True))
    else:
        asyncio.run(run_bot(with_schedule=False))


if __name__ == "__main__":
    main()
