"""브라우저에서 Threads 로그인을 마친 뒤 세션을 JSON으로 저장한다."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

import config
from services.playwright_launch import launch_chromium

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]


class LoginCancelled(RuntimeError):
    """로그인 전에 브라우저 창이 닫힘."""


def _ensure_stdio_utf8() -> None:
    """Windows 기본 cp949에서 안내 문구 출력이 깨지거나 예외가 나지 않게 한다."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _safe_print(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(message.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def _enable_windows_playwright() -> object | None:
    if sys.platform != "win32":
        return None
    previous = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        loop = asyncio.get_event_loop()
        if not loop.is_running():
            loop.close()
    except RuntimeError:
        pass
    asyncio.set_event_loop(asyncio.ProactorEventLoop())
    return previous


def _browser_was_closed(page, browser, closed: dict[str, bool]) -> bool:
    if closed["flag"]:
        return True
    try:
        if page.is_closed():
            return True
    except Exception:
        return True
    try:
        if not browser.is_connected():
            return True
    except Exception:
        return True
    return False


def _wait_until_logged_in(page, browser, timeout_seconds: int) -> None:
    closed = {"flag": False}

    def _mark_closed(*_args: object) -> None:
        closed["flag"] = True

    page.on("close", _mark_closed)
    page.context.on("close", _mark_closed)
    browser.on("disconnected", _mark_closed)

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _browser_was_closed(page, browser, closed):
            raise LoginCancelled("브라우저 창을 닫아서 로그인이 취소되었습니다.")
        remaining_ms = max(200, int((deadline - time.time()) * 1000))
        chunk_ms = min(1500, remaining_ms)
        try:
            page.wait_for_url(
                lambda value: "threads.com" in value.lower() and "/login" not in value.lower(),
                timeout=chunk_ms,
            )
            time.sleep(1.5)
            return
        except PlaywrightTimeoutError:
            continue
        except PlaywrightError as exc:
            if _browser_was_closed(page, browser, closed) or "closed" in str(exc).lower():
                raise LoginCancelled(
                    "브라우저 창을 닫아서 로그인이 취소되었습니다."
                ) from exc
            raise
    raise TimeoutError("제한 시간 안에 Threads 로그인을 확인하지 못했습니다.")


def save_threads_session(
    session_path: Path | None = None,
    *,
    wait_enter: bool | None = None,
    timeout_seconds: int = 180,
) -> Path:
    path = Path(session_path) if session_path else config.SESSION_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if wait_enter is None:
        wait_enter = sys.stdin.isatty()

    _ensure_stdio_utf8()
    previous = _enable_windows_playwright()
    try:
        with sync_playwright() as playwright:
            browser = launch_chromium(playwright, headless=False, args=LAUNCH_ARGS)
            context = browser.new_context(
                locale="ko-KR",
                timezone_id="Asia/Seoul",
                viewport={"width": 1440, "height": 900},
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            )
            page = context.new_page()
            _safe_print("Threads 로그인 페이지로 이동합니다...")
            page.goto("https://www.threads.com/login", wait_until="domcontentloaded")
            _safe_print("브라우저 창에서 Threads 로그인을 직접 완료해 주세요.")
            if wait_enter:
                _safe_print("피드가 보이면 이 터미널로 돌아와 Enter를 누르세요.")
                input()
            else:
                _safe_print("피드가 보이면 세션이 자동으로 저장됩니다. 창을 닫으면 취소됩니다.")
                _wait_until_logged_in(page, browser, timeout_seconds)

            context.storage_state(path=str(path))
            _safe_print(f"세션 저장 완료: {path}")
            browser.close()
    except LoginCancelled:
        raise
    except PlaywrightError as exc:
        message = str(exc).lower()
        if "closed" in message or "target" in message:
            raise LoginCancelled("브라우저 창을 닫아서 로그인이 취소되었습니다.") from exc
        raise
    finally:
        if previous is not None:
            asyncio.set_event_loop_policy(previous)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Threads 로그인 세션 저장")
    parser.add_argument("--path", default="", help="세션 JSON 저장 경로")
    parser.add_argument("--user", default="", help="대시보드 사용자 아이디")
    args = parser.parse_args()
    session_path: Path | None = None
    if args.path:
        session_path = Path(args.path)
    elif args.user:
        from services.auth import user_session_file

        session_path = user_session_file(args.user)
    save_threads_session(session_path)


if __name__ == "__main__":
    main()
