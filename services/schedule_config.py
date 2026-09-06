"""대시보드에서 저장하는 자동 발행 스케줄."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path

import config

logger = logging.getLogger(__name__)

WEEKDAY_NAMES = ("월", "화", "수", "목", "금", "토", "일")
CRON_DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MODES = ("review", "auto")
MODE_LABELS = {
    "review": "텔레그램 검토 후 발행",
    "auto": "승인 없이 바로 발행",
}


@dataclass
class PublishSchedule:
    enabled: bool = True
    timezone: str = "Asia/Seoul"
    weekdays: list[int] = field(default_factory=lambda: list(range(7)))
    times: list[str] = field(default_factory=lambda: ["09:00"])
    mode: str = "review"

    def cron_day_of_week(self) -> str:
        days = sorted({day for day in self.weekdays if 0 <= day <= 6})
        if not days:
            days = list(range(7))
        return ",".join(CRON_DOW[day] for day in days)

    def weekday_labels(self) -> str:
        days = sorted({day for day in self.weekdays if 0 <= day <= 6})
        if not days or len(days) == 7:
            return "매일"
        return ", ".join(WEEKDAY_NAMES[day] for day in days)

    def signature(self) -> str:
        payload = {
            "enabled": self.enabled,
            "timezone": self.timezone,
            "weekdays": sorted(self.weekdays),
            "times": list(self.times),
            "mode": self.mode,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def default_schedule() -> PublishSchedule:
    hour = max(0, min(23, config.SCHEDULE_HOUR))
    minute = max(0, min(59, config.SCHEDULE_MINUTE))
    timezone = config.TIMEZONE or "Asia/Seoul"
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        timezone = "Asia/Seoul"
    return PublishSchedule(
        enabled=True,
        timezone=timezone,
        weekdays=list(range(7)),
        times=[f"{hour:02d}:{minute:02d}"],
        mode="review",
    )


def parse_hm(value: str) -> time:
    text = (value or "").strip()
    hour_s, _, minute_s = text.partition(":")
    hour = int(hour_s)
    minute = int(minute_s or "0")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"시각이 올바르지 않습니다: {value}")
    return time(hour=hour, minute=minute)


def format_hm(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def _unique_times(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in values:
        stamp = format_hm(parse_hm(str(raw)))
        if stamp not in seen:
            seen.add(stamp)
            ordered.append(stamp)
    ordered.sort()
    return ordered or [format_hm(time(hour=config.SCHEDULE_HOUR, minute=config.SCHEDULE_MINUTE))]


def normalize_schedule(
    *,
    enabled: bool,
    timezone: str,
    weekdays: list[int],
    times: list[str],
    mode: str,
) -> PublishSchedule:
    tz = (timezone or "Asia/Seoul").strip() or "Asia/Seoul"
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"알 수 없는 타임존입니다: {tz}") from exc

    days = sorted({int(day) for day in weekdays if 0 <= int(day) <= 6})
    if not days:
        raise ValueError("요일을 하나 이상 선택하세요.")

    chosen_mode = (mode or "review").strip().lower()
    if chosen_mode not in MODES:
        raise ValueError("발행 방식은 검토 후 발행 또는 바로 발행만 가능합니다.")

    return PublishSchedule(
        enabled=bool(enabled),
        timezone=tz,
        weekdays=days,
        times=_unique_times(times),
        mode=chosen_mode,
    )


def collect_user_schedules() -> list[tuple[str, PublishSchedule]]:
    """대시보드 사용자별 스케줄. 계정이 없으면 전역 파일을 쓴다."""
    from services.auth import has_any_user, list_users, user_schedule_file

    if has_any_user():
        return [
            (user.username, load_schedule(user_schedule_file(user.username)))
            for user in list_users()
        ]
    return [("", load_schedule())]


def schedules_signature() -> str:
    return "|".join(
        f"{username}:{schedule.signature()}"
        for username, schedule in collect_user_schedules()
    )


def load_schedule(path: Path | None = None) -> PublishSchedule:
    path = path or config.SCHEDULE_FILE
    if not path.is_file():
        return default_schedule()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("스케줄 파일을 읽지 못해 기본값을 사용합니다: %s", path)
        return default_schedule()
    if not isinstance(raw, dict):
        return default_schedule()
    try:
        return normalize_schedule(
            enabled=bool(raw.get("enabled", True)),
            timezone=str(raw.get("timezone") or config.TIMEZONE or "Asia/Seoul"),
            weekdays=[int(day) for day in raw.get("weekdays", list(range(7)))],
            times=[str(item) for item in raw.get("times", [])] or default_schedule().times,
            mode=str(raw.get("mode") or "review"),
        )
    except (TypeError, ValueError):
        logger.exception("스케줄 파일이 올바르지 않아 기본값을 사용합니다: %s", path)
        return default_schedule()


def save_schedule(schedule: PublishSchedule, path: Path | None = None) -> None:
    normalized = normalize_schedule(
        enabled=schedule.enabled,
        timezone=schedule.timezone,
        weekdays=schedule.weekdays,
        times=schedule.times,
        mode=schedule.mode,
    )
    path = path or config.SCHEDULE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled": normalized.enabled,
        "timezone": normalized.timezone,
        "weekdays": normalized.weekdays,
        "times": normalized.times,
        "mode": normalized.mode,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def next_runs(schedule: PublishSchedule, count: int = 5) -> list[datetime]:
    if not schedule.enabled or count <= 0:
        return []
    tz = ZoneInfo(schedule.timezone)
    now = datetime.now(tz)
    allowed = set(schedule.weekdays)
    stamps = [parse_hm(item) for item in schedule.times]
    found: list[datetime] = []
    for offset in range(0, 21):
        day = now.date() + timedelta(days=offset)
        if day.weekday() not in allowed:
            continue
        for stamp in stamps:
            candidate = datetime.combine(day, stamp, tzinfo=tz)
            if candidate > now:
                found.append(candidate)
        if len(found) >= count:
            break
    return sorted(found)[:count]


def schedule_mtime() -> float:
    path = config.SCHEDULE_FILE
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
