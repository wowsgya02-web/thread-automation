"""Playwright로 threads.com에 본문을 발행하고, 첫 댓글로 서비스 링크를 남긴다."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright

import config
from models import PublishResult
from services.playwright_launch import launch_chromium

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'languages', {
  get: () => ['ko-KR', 'ko', 'en-US', 'en']
});
Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4, 5]
});
"""

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
]

POST_BUTTON_RE = re.compile(r"^(Post|게시)$", re.I)
REPLY_RE = re.compile(r"^(Reply|답글)$", re.I)
COMMENT_WAIT_SECONDS = 10


def resolve_session_path(session_path: Path | None = None) -> Path:
    candidates = [
        session_path,
        config.SESSION_PATH,
        config.ROOT / "session" / "threads_session.json",
        config.ROOT / "threads_session.json",
    ]
    users_root = getattr(config, "STATE_DIR", config.ROOT / "state") / "users"
    if users_root.is_dir():
        candidates.extend(sorted(users_root.glob("*/threads_session.json")))
    for path in candidates:
        if path is None:
            continue
        if path.exists() and path.stat().st_size > 20:
            return path
    raise FileNotFoundError(
        "Threads 세션을 찾을 수 없습니다. "
        "대시보드에서 브라우저로 Threads 로그인을 다시 진행하세요."
    )


def _enable_windows_playwright() -> object | None:
    """Windows에서 Playwright가 Chromium을 띄우려면 ProactorEventLoop가 필요하다.

    main.py가 텔레그램용 Selector 정책을 켜 두면 이 스레드의
    sync_playwright()가 NotImplementedError로 죽는다.
    """
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


def publish_thread(
    content: str,
    comment_url: str | None = None,
    session_path: Path | None = None,
) -> PublishResult:
    """동기 Playwright 발행. 텔레그램 핸들러에서는 asyncio.to_thread로 감싼다."""
    comment_url = (comment_url or config.SERVICE_URL or "").strip()
    session_path = resolve_session_path(session_path)
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    previous_policy = _enable_windows_playwright()
    logger.info("Threads 발행 시작 (session=%s, headless=%s)", session_path, config.HEADLESS)
    try:
        return _run_publish(content, comment_url, session_path)
    finally:
        if previous_policy is not None:
            asyncio.set_event_loop_policy(previous_policy)


def _run_publish(content: str, comment_url: str, session_path: Path) -> PublishResult:
    page: Page | None = None
    with sync_playwright() as playwright:
        browser = launch_chromium(
            playwright,
            headless=config.HEADLESS,
            args=LAUNCH_ARGS,
            slow_mo=config.PLAYWRIGHT_SLOW_MO or None,
        )
        context = browser.new_context(
            storage_state=str(session_path),
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            color_scheme="light",
        )
        context.add_init_script(STEALTH_INIT_SCRIPT)
        page = context.new_page()
        try:
            _open_home(page)
            _assert_logged_in(page)
            _open_composer(page)
            _fill_composer(page, content)
            _click_post(page)
            logger.info("본문 게시 클릭 완료. %s초 대기 후 댓글 작성", COMMENT_WAIT_SECONDS)
            time.sleep(COMMENT_WAIT_SECONDS)

            post_url = ""
            comment_ok = False
            comment_error = ""
            if comment_url:
                try:
                    post_url, comment_ok = _comment_on_latest(page, content, comment_url)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("본문은 발행됐지만 댓글 등록에 실패했습니다")
                    comment_error = str(exc)
                    post_url = _try_open_own_latest(page, content)
            else:
                logger.warning("SERVICE_URL이 비어 댓글을 건너뜁니다.")
                post_url = _try_open_own_latest(page, content)

            screenshot = _screenshot(page, "publish_ok")
            return PublishResult(
                success=True,
                post_url=post_url,
                comment_ok=comment_ok if comment_url else True,
                error=comment_error,
                screenshot_path=screenshot,
            )
        except Exception as exc:
            logger.exception("Threads 발행 실패")
            screenshot = _screenshot(page, "publish_error") if page else ""
            return PublishResult(
                success=False,
                error=str(exc),
                screenshot_path=screenshot,
            )
        finally:
            context.close()
            browser.close()


def _open_home(page: Page) -> None:
    urls = [config.THREADS_BASE_URL, "https://www.threads.net"]
    last_error: Exception | None = None
    for url in urls:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(1500)
            if "login" not in page.url.lower():
                return
            last_error = RuntimeError(f"로그인 페이지로 이동됨: {page.url}")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    if last_error:
        raise last_error


def _assert_logged_in(page: Page) -> None:
    url = page.url.lower()
    if any(token in url for token in ("/login", "checkpoint", "challenge")):
        raise RuntimeError(
            "세션이 만료되었거나 추가 인증이 필요합니다. "
            "`python main.py login`으로 세션을 다시 저장하세요."
        )


def _open_composer(page: Page) -> None:
    _human_pause()
    candidates = [
        page.get_by_text("What's new?", exact=False),
        page.get_by_text("새로운 소식", exact=False),
        page.get_by_text("Start a thread", exact=False),
        page.get_by_text("스레드 시작", exact=False),
        page.get_by_role("button", name=re.compile(r"new thread|새 스레드|create", re.I)),
        page.locator('svg[aria-label="Create"]').locator("xpath=ancestor::*[@role='link' or @role='button'][1]"),
    ]
    if _click_first(candidates, timeout_ms=4_000):
        editor = page.locator('div[role="dialog"] [contenteditable="true"]').first
        try:
            editor.wait_for(state="visible", timeout=8_000)
            return
        except PlaywrightTimeout:
            logger.info("작성 다이얼로그가 바로 열리지 않아 재시도합니다.")

    page.keyboard.press("c")
    editor = page.locator('div[role="dialog"] [contenteditable="true"]').first
    editor.wait_for(state="visible", timeout=10_000)


def _fill_composer(page: Page, content: str) -> None:
    editor = page.locator('div[role="dialog"] [contenteditable="true"]').first
    editor.click()
    _human_pause(0.2, 0.5)
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    delay = random.randint(12, 28)
    editor.type(content, delay=delay)
    page.wait_for_timeout(400)
    current = (editor.inner_text() or "").strip()
    if content.strip()[:20] not in current and current[:20] not in content:
        logger.warning("type() 결과가 비어 보여 insert_text로 재입력합니다.")
        editor.click()
        page.keyboard.insert_text(content)


def _click_post(page: Page) -> None:
    dialog = page.locator('div[role="dialog"]')
    button = dialog.get_by_role("button", name=POST_BUTTON_RE).last
    button.wait_for(state="visible", timeout=10_000)

    try:
        page.wait_for_function(
            """() => {
                const dlg = document.querySelector('div[role="dialog"]');
                if (!dlg) return false;
                const nodes = [...dlg.querySelectorAll('div[role="button"], button')];
                const btn = nodes.find((el) => /^(Post|게시)$/i.test((el.innerText || '').trim()));
                if (!btn) return false;
                const aria = btn.getAttribute('aria-disabled');
                return aria !== 'true' && !btn.hasAttribute('disabled');
            }""",
            timeout=12_000,
        )
    except PlaywrightTimeout:
        logger.warning("게시 버튼 활성 대기 시간 초과 — 클릭을 시도합니다.")

    _human_pause(0.3, 0.8)
    button.click()

    try:
        page.locator('div[role="dialog"] [contenteditable="true"]').first.wait_for(
            state="hidden",
            timeout=20_000,
        )
    except PlaywrightTimeout as exc:
        raise RuntimeError("게시 버튼을 눌렀지만 작성 창이 닫히지 않았습니다.") from exc


def _comment_on_latest(page: Page, content: str, comment_url: str) -> tuple[str, bool]:
    snippet = _snippet(content)
    post_url = _focus_own_post(page, snippet)
    _human_pause(0.4, 0.9)
    if not _open_reply_box(page):
        raise RuntimeError("댓글 입력창을 열지 못했습니다.")
    _fill_reply(page, comment_url)
    _submit_reply(page)
    logger.info("댓글 등록 시도 완료 (post_url=%s)", post_url)
    return post_url, True


def _try_open_own_latest(page: Page, content: str) -> str:
    try:
        return _focus_own_post(page, _snippet(content))
    except Exception:
        return page.url


def _focus_own_post(page: Page, snippet: str) -> str:
    article = _find_article(page, snippet)
    if article is not None:
        link = article.locator('a[href*="/post/"]').first
        if link.count() > 0:
            link.click()
            page.wait_for_timeout(1500)
            return page.url
        article.click()
        page.wait_for_timeout(1500)
        return page.url

    _open_own_profile(page)
    article = _find_article(page, snippet)
    if article is None:
        article = page.locator('[role="article"]').first
        if article.count() == 0:
            raise RuntimeError("발행한 게시물을 피드/프로필에서 찾지 못했습니다.")
    link = article.locator('a[href*="/post/"]').first
    if link.count() > 0:
        link.click()
    else:
        article.click()
    page.wait_for_timeout(1500)
    return page.url


def _find_article(page: Page, snippet: str):
    articles = page.locator('[role="article"]')
    try:
        count = min(articles.count(), 12)
    except Exception:
        return None
    for index in range(count):
        node = articles.nth(index)
        try:
            text = node.inner_text(timeout=2_000)
        except Exception:
            continue
        if snippet and snippet in text.replace("\n", " "):
            return node
        if snippet and snippet[:18] in text:
            return node
    return None


def _open_own_profile(page: Page) -> None:
    profile = page.locator('nav a[href^="/@"], header a[href^="/@"], a[href^="/@"]').first
    profile.wait_for(state="visible", timeout=8_000)
    profile.click()
    page.wait_for_timeout(2000)


def _open_reply_box(page: Page) -> bool:
    candidates = [
        page.locator('svg[aria-label="Reply"]'),
        page.locator('svg[aria-label="답글"]'),
        page.get_by_role("button", name=REPLY_RE),
        page.get_by_text("Write a reply", exact=False),
        page.get_by_text("답글을 입력", exact=False),
    ]
    if _click_first(candidates, timeout_ms=3_500):
        editor = page.locator('[contenteditable="true"]').last
        try:
            editor.wait_for(state="visible", timeout=8_000)
            return True
        except PlaywrightTimeout:
            return False
    editor = page.locator('[contenteditable="true"]').last
    try:
        editor.wait_for(state="visible", timeout=5_000)
        editor.click()
        return True
    except PlaywrightTimeout:
        return False


def _fill_reply(page: Page, comment_url: str) -> None:
    editor = page.locator('[contenteditable="true"]').last
    editor.click()
    _human_pause(0.2, 0.4)
    editor.type(comment_url, delay=random.randint(15, 32))


def _submit_reply(page: Page) -> None:
    dialog = page.locator('div[role="dialog"]')
    button = dialog.get_by_role("button", name=POST_BUTTON_RE)
    if button.count() == 0:
        button = page.get_by_role("button", name=POST_BUTTON_RE)
    button.last.click()
    page.wait_for_timeout(2000)


def _click_first(locators, timeout_ms: int) -> bool:
    for locator in locators:
        try:
            target = locator.first
            target.wait_for(state="visible", timeout=timeout_ms)
            target.click()
            return True
        except Exception:
            continue
    return False


def _snippet(content: str) -> str:
    line = next((ln.strip() for ln in content.splitlines() if ln.strip()), content.strip())
    return line[:48]


def _human_pause(low: float = 0.35, high: float = 1.1) -> None:
    time.sleep(random.uniform(low, high))


def _screenshot(page: Page, label: str) -> str:
    path = config.LOGS_DIR / f"{label}_{int(time.time())}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception:
        return ""
