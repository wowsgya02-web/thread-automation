"""Playwright 브라우저 실행. 설치된 Edge/Chrome을 우선하고, 샌드박스 경로를 피한다."""

from __future__ import annotations

import os


def prepare_browser_env() -> None:
    raw = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if "cursor-sandbox-cache" in raw.replace("\\", "/").lower():
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)


def launch_chromium(playwright, *, headless: bool, args: list[str], slow_mo: int | None = None):
    prepare_browser_env()
    errors: list[str] = []
    for extra in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
        try:
            return playwright.chromium.launch(
                headless=headless,
                args=args,
                slow_mo=slow_mo or None,
                **extra,
            )
        except Exception as exc:
            label = extra.get("channel") if extra else "playwright-chromium"
            errors.append(f"{label}: {exc}")
    joined = "\n".join(errors)
    raise RuntimeError(
        "브라우저를 실행하지 못했습니다. Edge 또는 Chrome이 설치돼 있는지 확인하거나 "
        "`python -m playwright install chromium`을 실행해 주세요.\n"
        f"{joined}"
    )
