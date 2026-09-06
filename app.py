"""Threads 마케팅 파이프라인 Streamlit 대시보드."""

from __future__ import annotations

import os
import sys
from datetime import date, time as dt_time
from pathlib import Path

import streamlit as st

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import config

from importlib import reload
import services.auth as auth_service

reload(auth_service)
from services.auth import (
    User,
    create_user,
    ensure_member_docs,
    has_any_user,
    list_users,
    load_launch_date,
    migrate_legacy_files,
    save_launch_date,
    user_docs_dir,
    user_schedule_file,
    verify_user,
)
from services.content_generator import generate_post, rewrite_for_threads
from database.db_manager import get_db
from services.telegram_reviewer import send_for_review_sync, telegram_configured
from services.threads_api import (
    credentials_ok,
    load_credentials,
    save_credentials,
    verify_token,
    clear_credentials,
)
from services.doc_analyzer import (
    analyze_or_fallback,
    clear_analysis_cache,
    generate_pain_points_markdown,
)
from services.schedule_config import (
    MODE_LABELS,
    WEEKDAY_NAMES,
    format_hm,
    load_schedule,
    next_runs,
    normalize_schedule,
    parse_hm,
    save_schedule,
)

st.set_page_config(page_title="Threads 마케팅 파이프라인 대시보드", layout="wide")


def current_phase(diff_days: int) -> str:
    if diff_days > 21:
        return "W-4: 고객 문제 정의 & 공감대 형성 (Awareness)"
    if diff_days > 14:
        return "W-3: 빌드 인 퍼블릭 & 가치 제안 (Interest)"
    if diff_days > 7:
        return "W-2: 제품 시연 & 사전 대기자 모집 (Waitlist)"
    if diff_days > 0:
        return "W-1: ASO & 최종 카운트다운 (Pre-Launch)"
    if diff_days == 0:
        return "D-Day: 정식 런칭 (Acquisition & Activation)"
    return "W+1~W+2: 데이터 분석 & 리텐션 최적화 (Retention & Referral)"


DOC_LABELS = {
    "strategy.md": "마케팅 전략",
    "service.md": "서비스 특장점",
    "pain_points.md": "고충 인벤토리",
    "sample_service.md": "샘플 서비스",
}
DOC_PRIORITY = ("strategy.md", "service.md", "pain_points.md")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="cp949", errors="replace")


def list_dashboard_docs(user: User) -> list[Path]:
    if user.is_admin:
        directory = config.DOCS_DIR
        names = DOC_PRIORITY
    else:
        directory = ensure_member_docs(user.username)
        names = ("service.md", "pain_points.md")
    files = []
    for name in names:
        path = directory / name
        if path.is_file():
            files.append(path)
    return files


def docs_dir_for(user: User) -> Path:
    if user.is_admin:
        return config.DOCS_DIR
    return user_docs_dir(user.username)


def analyze_for_user(user: User):
    return analyze_or_fallback(
        docs_dir=docs_dir_for(user),
        use_generic_strategy=not user.is_admin,
    )


def doc_label(path: Path) -> str:
    return DOC_LABELS.get(path.name, path.stem)


def selected_doc_name(docs: list[Path]) -> str:
    raw = st.query_params.get("doc") or st.session_state.get("active_doc") or ""
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    requested = str(raw)
    names = {path.name for path in docs}
    if requested in names:
        return requested
    return docs[0].name if docs else ""


_SCHED_META_KEYS = {"sched_form_loaded", "sched_form_user"}


def _reset_schedule_form(saved) -> None:
    for key in list(st.session_state.keys()):
        if str(key).startswith("sched_") and key not in _SCHED_META_KEYS:
            del st.session_state[key]
    st.session_state.sched_enabled = saved.enabled
    st.session_state.sched_mode = saved.mode
    st.session_state.sched_weekdays = list(saved.weekdays)
    st.session_state.sched_timezone = saved.timezone
    st.session_state.sched_time_count = max(1, len(saved.times))
    for index, stamp in enumerate(saved.times):
        st.session_state[f"sched_t_{index}"] = parse_hm(stamp)
    st.session_state.sched_form_loaded = True


def _collect_schedule_from_form():
    count = int(st.session_state.get("sched_time_count") or 1)
    times: list[str] = []
    for index in range(count):
        value = st.session_state.get(f"sched_t_{index}")
        if isinstance(value, dt_time):
            times.append(format_hm(value))
        elif value:
            times.append(str(value))
    return normalize_schedule(
        enabled=bool(st.session_state.get("sched_enabled", True)),
        timezone=str(st.session_state.get("sched_timezone") or "Asia/Seoul"),
        weekdays=list(st.session_state.get("sched_weekdays") or list(range(7))),
        times=times,
        mode=str(st.session_state.get("sched_mode") or "review"),
    )


def _session_user() -> User | None:
    raw = st.session_state.get("auth_user")
    if not isinstance(raw, dict) or not raw.get("username"):
        return None
    return User(username=str(raw["username"]), role=str(raw.get("role") or "member"))


def _set_session_user(user: User) -> None:
    st.session_state.auth_user = {"username": user.username, "role": user.role}
    migrate_legacy_files(user.username)


@st.dialog("Threads API는 이렇게 연결해요", width="large")
def _threads_guide_dialog() -> None:
    st.markdown(
        """
브라우저 로그인 대신 **Meta Threads API 토큰**으로 연결합니다.  
Streamlit Cloud에서도 로컬 PC 없이 발행할 수 있습니다.

---

### 준비 (처음 한 번)

1. [Meta for Developers](https://developers.facebook.com/apps/)에서 앱을 만듭니다.
2. 앱에 **Threads API** 사용 사례를 추가합니다.
3. 권한: `threads_basic`, `threads_content_publish`  
   (첫 댓글 링크까지 쓰려면 `threads_manage_replies`도)
4. Threads 테스트 사용자/본인 계정으로 로그인 토큰을 발급받습니다.
5. **장기 토큰(Long-lived)** 으로 바꿔 두는 것을 권장합니다.

---

### 대시보드에서

1. Access Token을 붙여 넣고 **토큰 확인 & 저장**을 누릅니다.
2. 사용자 ID는 토큰 확인 시 자동으로 채워집니다.
3. **Threads API가 연결되었습니다**가 나오면 끝입니다.

토큰은 비밀번호처럼 다루세요. 다른 사람에게 공유하면 안 됩니다.
"""
    )
    if st.button("닫기", type="primary", use_container_width=True):
        st.rerun()


def require_login() -> User:
    user = _session_user()
    if user is not None:
        return user

    st.title("Threads 마케팅 파이프라인")
    st.caption("계정으로 로그인한 뒤 대시보드를 사용할 수 있습니다.")

    if not has_any_user():
        st.info("처음 사용입니다. 웹에 올릴 관리자 계정을 만드세요.")
        with st.form("setup_admin"):
            username = st.text_input("관리자 아이디")
            password = st.text_input("비밀번호", type="password")
            confirm = st.text_input("비밀번호 확인", type="password")
            submitted = st.form_submit_button("관리자 만들기", type="primary")
        if submitted:
            try:
                if password != confirm:
                    raise ValueError("비밀번호 확인이 일치하지 않습니다.")
                created = create_user(username, password, role="admin")
                _set_session_user(created)
                st.success("관리자 계정을 만들었습니다.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
        st.stop()

    with st.form("login"):
        username = st.text_input("아이디")
        password = st.text_input("비밀번호", type="password")
        submitted = st.form_submit_button("로그인", type="primary")
    if submitted:
        found = verify_user(username, password)
        if found is None:
            st.error("아이디 또는 비밀번호가 올바르지 않습니다.")
        else:
            _set_session_user(found)
            st.rerun()
    st.stop()
    raise RuntimeError("unreachable")


current_user = require_login()
schedule_path = user_schedule_file(current_user.username)

st.title("🚀 Threads 마케팅 자동화 파이프라인")
st.caption("고객 문제 중심의 8단계 퍼널 & 반자동 스레드 퍼블리셔")

st.sidebar.markdown(f"**{current_user.username}** · {('관리자' if current_user.is_admin else '멤버')}")
if st.sidebar.button("로그아웃", use_container_width=True):
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.rerun()

st.sidebar.header("⚙️ 런칭 스케줄 설정")
if "launch_date" not in st.session_state:
    st.session_state.launch_date = load_launch_date(current_user.username)
launch_date = st.sidebar.date_input("서비스 런칭 목표일", key="launch_date")
save_launch_date(current_user.username, launch_date)
today = date.today()
diff_days = (launch_date - today).days

if diff_days > 0:
    st.sidebar.markdown(f"### **D-Day:** `D-{diff_days}`")
elif diff_days == 0:
    st.sidebar.markdown("### **D-Day:** `D-Day (오늘 출시!)`")
else:
    st.sidebar.markdown(f"### **D-Day:** `D+{abs(diff_days)}`")

phase = current_phase(diff_days)
st.sidebar.info(f"**현재 실행 페이즈:**\n\n{phase}")

saved_schedule = load_schedule(schedule_path)
st.sidebar.divider()
st.sidebar.header("📅 자동 발행")
if saved_schedule.enabled:
    upcoming = next_runs(saved_schedule, 1)
    next_label = upcoming[0].strftime("%m/%d %H:%M") if upcoming else "예정 없음"
    st.sidebar.success(
        f"{MODE_LABELS.get(saved_schedule.mode, saved_schedule.mode)}\n"
        f"{saved_schedule.weekday_labels()} · {', '.join(saved_schedule.times)}"
    )
    st.sidebar.caption(f"다음 실행: {next_label} ({saved_schedule.timezone})")
else:
    st.sidebar.warning("자동 발행이 꺼져 있습니다.")
st.sidebar.caption("예약 발행과 텔레그램 승인 버튼을 쓰려면 `python main.py schedule`을 켜 두세요.")
if telegram_configured():
    st.sidebar.success("텔레그램 알림이 연결되어 있습니다.")
else:
    st.sidebar.error("텔레그램 알림이 설정되지 않았습니다. .env의 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID를 확인하세요.")

if current_user.is_admin:
    st.sidebar.divider()
    st.sidebar.header("👥 사용자")
    with st.sidebar.form("create_member"):
        new_name = st.text_input("새 아이디")
        new_password = st.text_input("비밀번호", type="password")
        new_role = st.selectbox("역할", options=["member", "admin"], format_func=lambda value: "관리자" if value == "admin" else "멤버")
        created = st.form_submit_button("계정 추가")
    if created:
        try:
            create_user(new_name, new_password, role=str(new_role))
            st.sidebar.success(f"`{new_name}` 계정을 만들었습니다.")
        except Exception as exc:
            st.sidebar.error(str(exc))
    others = [user.username for user in list_users()]
    if others:
        st.sidebar.caption("등록된 계정: " + ", ".join(others))

docs = list_dashboard_docs(current_user)
active_name = selected_doc_name(docs)
if active_name:
    st.session_state.active_doc = active_name
if "docs_open" not in st.session_state:
    st.session_state.docs_open = False

title_col, toggle_col, _ = st.columns([1.4, 1, 4], vertical_alignment="bottom")
with title_col:
    st.subheader("문서")
with toggle_col:
    toggle_label = "접기" if st.session_state.docs_open else "펴기"
    if st.button(toggle_label, use_container_width=True, key="docs_toggle"):
        st.session_state.docs_open = not st.session_state.docs_open
        st.rerun()

if not st.session_state.docs_open:
    if current_user.is_admin:
        st.caption("문서를 보거나 수정하려면 펴기를 누르세요.")
    else:
        st.caption("서비스 특장점을 올리려면 펴기를 누르세요. 마케팅 전략은 멤버에게 보이지 않습니다.")
else:
    if not docs:
        st.info("표시할 문서가 없습니다.")
    else:
        if not st.query_params.get("doc") and active_name:
            st.query_params["doc"] = active_name
        link_cols = st.columns(len(docs))
        for column, path in zip(link_cols, docs):
            with column:
                is_active = path.name == active_name
                if st.button(
                    doc_label(path),
                    key=f"doc_nav_{path.stem}",
                    type="primary" if is_active else "tertiary",
                    use_container_width=True,
                ):
                    st.session_state.active_doc = path.name
                    st.query_params["doc"] = path.name
                    st.rerun()
        if current_user.is_admin:
            st.caption("문서를 누르면 아래에서 바로 수정할 수 있습니다.")
        else:
            st.caption("서비스 특장점을 저장하면 고충 인벤토리를 API로 다시 채웁니다.")

    active_path = next((path for path in docs if path.name == active_name), None)
    if docs and active_path is None:
        st.info("위에서 문서를 선택하면 내용을 편집할 수 있습니다.")
    elif active_path is not None:
        file_text = _read_text(active_path) if active_path.is_file() else ""
        revision = int(st.session_state.get("doc_rev", 0))
        st.markdown(f"**{doc_label(active_path)}** `{active_path.name}` · {len(file_text):,}자")
        if not file_text.strip():
            st.warning("아직 내용이 없습니다. 파일을 올리거나 아래에 입력하세요.")

        is_member_service = (not current_user.is_admin) and active_path.name == "service.md"
        is_member_pain = (not current_user.is_admin) and active_path.name == "pain_points.md"
        form_key = f"doc_form_{current_user.username}_{active_path.stem}_{revision}"
        with st.form(key=form_key, border=False):
            uploaded = None
            if is_member_service:
                uploaded = st.file_uploader(
                    "서비스 소개 파일 업로드 (.md, .txt)",
                    type=["md", "txt"],
                )
            body = st.text_area(
                "문서 내용",
                value=file_text,
                height=420,
                label_visibility="collapsed",
            )
            save_label = "저장하고 고충 분석" if is_member_service else "문서 저장"
            save_col, reload_col, extra_col = st.columns([1, 1, 1])
            with save_col:
                saved = st.form_submit_button(save_label, type="primary", use_container_width=True)
            with reload_col:
                reloaded = st.form_submit_button("파일에서 다시 불러오기", use_container_width=True)
            with extra_col:
                regen_pain = False
                if is_member_pain:
                    regen_pain = st.form_submit_button("서비스 다시 분석", use_container_width=True)

        if saved:
            content = body
            if uploaded is not None:
                content = uploaded.getvalue().decode("utf-8", errors="replace")
            active_path.write_text(content, encoding="utf-8")
            clear_analysis_cache()
            if is_member_service:
                try:
                    with st.spinner("서비스 특장점을 분석해 고충 인벤토리를 채우는 중..."):
                        pain_md = generate_pain_points_markdown(content)
                    pain_path = docs_dir_for(current_user) / "pain_points.md"
                    pain_path.write_text(pain_md, encoding="utf-8")
                    st.success("서비스 문서를 저장했고, 고충 인벤토리를 자동으로 채웠습니다.")
                except Exception as exc:
                    st.warning(f"서비스 문서는 저장했습니다. 고충 분석은 실패했습니다: {exc}")
            else:
                st.success(f"`{active_path.name}`을(를) 저장했습니다. ({len(content):,}자)")
            st.session_state.doc_rev = revision + 1
            st.rerun()
        elif regen_pain:
            service_path = docs_dir_for(current_user) / "service.md"
            service_text = _read_text(service_path) if service_path.is_file() else ""
            try:
                with st.spinner("서비스 특장점을 분석해 고충 인벤토리를 채우는 중..."):
                    pain_md = generate_pain_points_markdown(service_text)
                active_path.write_text(pain_md, encoding="utf-8")
                clear_analysis_cache()
                st.success("고충 인벤토리를 다시 채웠습니다.")
                st.session_state.doc_rev = revision + 1
                st.rerun()
            except Exception as exc:
                st.error(f"고충 분석 실패: {exc}")
        elif reloaded:
            st.session_state.doc_rev = revision + 1
            st.rerun()

st.divider()
st.subheader("Threads 계정")
guide_col, _ = st.columns([1, 3])
with guide_col:
    if st.button("연결 방법 보기", use_container_width=True):
        _threads_guide_dialog()

st.caption(
    "대시보드 로그인과 별개입니다. Meta Threads API 토큰으로 연결합니다. "
    "브라우저/로컬 PC가 필요 없습니다."
)

threads_ok = credentials_ok(current_user.username)
saved_creds = load_credentials(current_user.username)
if threads_ok:
    handle = saved_creds.get("threads_username") or saved_creds.get("user_id")
    st.success(f"Threads API가 연결되어 있습니다. (@{handle})" if handle else "Threads API가 연결되어 있습니다.")
else:
    st.warning("아직 Threads API 토큰이 없습니다. 아래에서 Access Token을 연결하세요.")

st.caption(
    "Streamlit Cloud는 재배포 시 파일이 초기화될 수 있습니다. "
    "안정적으로 쓰려면 App settings → Secrets에 "
    "`THREADS_ACCESS_TOKEN`, `THREADS_USER_ID`도 넣어 주세요."
)

with st.form("threads_api_connect"):
    token_input = st.text_input(
        "Access Token",
        type="password",
        help="Meta 개발자 앱에서 발급한 Threads 사용자 토큰",
    )
    user_id_input = st.text_input(
        "Threads User ID (비우면 토큰으로 자동 조회)",
        value=str(saved_creds.get("user_id") or ""),
    )
    save_clicked = st.form_submit_button("토큰 확인 & 저장", type="primary", use_container_width=True)

if save_clicked:
    try:
        if not (token_input or "").strip():
            raise ValueError("Access Token을 입력하세요.")
        profile = verify_token(token_input.strip())
        user_id = (user_id_input or "").strip() or str(profile.get("id") or "")
        if not user_id:
            raise ValueError("User ID를 확인하지 못했습니다.")
        save_credentials(
            token_input.strip(),
            user_id,
            username=current_user.username,
            threads_username=str(profile.get("username") or ""),
        )
        st.success(f"연결되었습니다. @{profile.get('username') or user_id}")
        st.rerun()
    except Exception as exc:
        st.error(f"토큰 연결 실패: {exc}")

if threads_ok and st.button("Threads API 연결 해제", use_container_width=True):
    clear_credentials(current_user.username)
    st.rerun()

st.divider()
st.subheader("✍️ 스레드 초안 실시간 생성 & 프리뷰")
st.caption("초안을 뽑으면 텔레그램으로 검토 알림이 갑니다. 승인 버튼을 쓰려면 `python main.py schedule`을 클라우드/서버에서 켜 두세요 (브라우저 불필요).")

if st.button("새로운 스레드 초안 뽑기", use_container_width=True):
    spinner = (
        "마케팅 전략 앵글에 맞춰 글 쓰는 중..."
        if current_user.is_admin
        else "서비스·고충 문서에 맞춰 글 쓰는 중..."
    )
    with st.spinner(spinner):
        try:
            doc_data = analyze_for_user(current_user)
            post = generate_post(
                doc_data,
                phase=phase,
                extra_instructions=(
                    f"현재 마케팅 페이즈: {phase}. "
                    "이 단계의 목표에 맞는 훅과 소구점 1가지만 사용할 것."
                ),
            )
            st.session_state["latest_post"] = post.content
            st.session_state["latest_meta"] = (
                f"{post.persona} · {post.pain_point} · {post.topic}"
            )
            st.session_state["latest_draft"] = {
                "topic": post.topic,
                "pain_point": post.pain_point,
                "persona": post.persona,
                "content": post.content,
            }
        except Exception as exc:
            st.error(f"초안 생성 실패: {exc}")
        else:
            try:
                if not telegram_configured():
                    st.warning(
                        "글은 만들었지만 텔레그램이 설정되지 않아 알림을 보내지 못했습니다. "
                        ".env의 TELEGRAM_BOT_TOKEN과 TELEGRAM_CHAT_ID를 확인하세요."
                    )
                else:
                    record_id = get_db().insert_post(post, status="pending")
                    record = get_db().get_post(record_id)
                    if record is None:
                        raise RuntimeError("초안 저장 후 레코드를 읽지 못했습니다.")
                    send_for_review_sync(
                        record,
                        persona=post.persona,
                        analysis=doc_data,
                    )
                    st.success("텔레그램으로 초안 알림을 보냈습니다.")
            except Exception as exc:
                st.warning(f"글은 만들었지만 텔레그램 알림에 실패했습니다: {exc}")

preview_text = st.session_state.get(
    "latest_post", "위 버튼을 누르면 전략에 맞춘 초안이 생성됩니다."
)
st.text_area("생성된 글 미리보기", value=preview_text, height=220)
if st.session_state.get("latest_meta"):
    st.caption(st.session_state["latest_meta"])

st.divider()
st.subheader("🪄 내 글을 Threads 인기글 구조로 바꾸기")
st.caption("원하는 내용을 넣으면, 스크롤을 멈추는 훅 + 공감 장면 + 반전/인사이트 구조의 스레드 말투로 다시 씁니다. 사실관계는 유지합니다.")

rewrite_col, result_col = st.columns([1, 1])
with rewrite_col:
    source_text = st.text_area(
        "원문 입력",
        height=220,
        placeholder="예: 강아지랑 같이 갈 수 있는 카페를 지도로 모아두는 서비스를 만들고 있어요. 다음 달에 나옵니다.",
        key="rewrite_source",
    )
    use_strategy = st.checkbox(
        "문서 톤·금지어 반영" if current_user.is_admin else "서비스·고충 문서 반영",
        value=True,
    )
    if st.button("Threads 말투로 바꾸기", use_container_width=True, type="primary"):
        if not (source_text or "").strip():
            st.warning("바꿀 글을 입력하세요.")
        else:
            with st.spinner("훅과 인기글 구조로 다시 쓰는 중..."):
                try:
                    analysis = analyze_for_user(current_user) if use_strategy else None
                    rewritten = rewrite_for_threads(
                        source_text,
                        analysis=analysis,
                        phase=phase,
                        extra_instructions=(
                            f"현재 마케팅 페이즈: {phase}. "
                            "페이즈 목표에 맞는 훅 강도로 조절할 것."
                        ),
                    )
                    st.session_state["rewritten_post"] = rewritten.content
                    st.session_state["rewritten_meta"] = (
                        f"{rewritten.persona} · {rewritten.pain_point} · {rewritten.topic}"
                    )
                except Exception as exc:
                    st.error(f"리라이트 실패: {exc}")

with result_col:
    rewritten_text = st.session_state.get(
        "rewritten_post", "왼쪽 원문을 넣고 버튼을 누르면 여기에 변환 결과가 나옵니다."
    )
    st.text_area("변환 결과", value=rewritten_text, height=220)
    if st.session_state.get("rewritten_meta"):
        st.caption(st.session_state["rewritten_meta"])
    if st.session_state.get("rewritten_post"):
        if st.button("이 결과를 위 초안 미리보기로 복사"):
            st.session_state["latest_post"] = st.session_state["rewritten_post"]
            st.session_state["latest_meta"] = st.session_state.get("rewritten_meta", "")
            st.rerun()

st.divider()
st.subheader("📅 자동 발행 스케줄")
st.caption(
    "요일과 시각을 저장하면 `python main.py schedule`이 20초 안에 반영합니다. "
    "Streamlit만 켜 두면 예약 시각에 발행되지 않습니다."
)

if st.session_state.pop("publish_schedule_flash", None):
    st.success("저장했습니다. `python main.py schedule`이 켜져 있으면 약 20초 안에 반영됩니다.")

if st.session_state.get("sched_form_user") != current_user.username:
    st.session_state.sched_form_loaded = False
    st.session_state.sched_form_user = current_user.username

if not st.session_state.get("sched_form_loaded"):
    _reset_schedule_form(saved_schedule)

sched_left, sched_right = st.columns([1.2, 1])
with sched_left:
    st.toggle("자동 발행 사용", key="sched_enabled")
    st.radio(
        "발행 방식",
        options=["review", "auto"],
        format_func=lambda value: MODE_LABELS.get(value, value),
        key="sched_mode",
        horizontal=True,
    )
    if st.session_state.get("sched_mode") == "auto":
        st.warning(
            "승인 없이 Threads에 바로 올립니다. API 토큰이 연결되어 있어야 하고, "
            "초안을 텔레그램에서 고칠 기회는 없습니다."
        )
        if not threads_ok:
            st.error("위에서 Threads API 토큰을 먼저 연결하세요.")

    st.multiselect(
        "요일",
        options=list(range(7)),
        format_func=lambda day: WEEKDAY_NAMES[day],
        key="sched_weekdays",
    )
    st.text_input("타임존", key="sched_timezone")
    st.number_input(
        "하루 발행 횟수",
        min_value=1,
        max_value=8,
        step=1,
        key="sched_time_count",
    )

    time_count = int(st.session_state.get("sched_time_count") or 1)
    time_cols = st.columns(min(time_count, 4))
    for index in range(time_count):
        slot_key = f"sched_t_{index}"
        if slot_key not in st.session_state:
            used_hours = set()
            for earlier in range(index):
                earlier_value = st.session_state.get(f"sched_t_{earlier}")
                if isinstance(earlier_value, dt_time):
                    used_hours.add(earlier_value.hour)
            hour = 21
            while hour in used_hours:
                hour = (hour + 1) % 24
            st.session_state[slot_key] = parse_hm(f"{hour:02d}:00")
        with time_cols[index % 4]:
            st.time_input(f"{index + 1}회차", key=slot_key)

    save_col, reload_col = st.columns(2)
    with save_col:
        if st.button("스케줄 저장", type="primary", use_container_width=True):
            try:
                draft_schedule = _collect_schedule_from_form()
                save_schedule(draft_schedule, schedule_path)
                st.session_state.publish_schedule_flash = True
                st.session_state.sched_form_loaded = False
                st.rerun()
            except Exception as exc:
                st.error(f"저장 실패: {exc}")
    with reload_col:
        if st.button("저장된 설정 다시 불러오기", use_container_width=True):
            st.session_state.sched_form_loaded = False
            st.rerun()

with sched_right:
    try:
        preview = _collect_schedule_from_form()
    except Exception:
        preview = saved_schedule
    st.markdown("**다음 실행 예정**")
    if not preview.enabled:
        st.info("자동 발행이 꺼져 있으면 예약되지 않습니다.")
    else:
        upcoming_list = next_runs(preview, 5)
        if not upcoming_list:
            st.info("앞으로 21일 안에 맞는 시각이 없습니다. 요일이나 시각을 확인하세요.")
        else:
            for when in upcoming_list:
                st.write(f"- {when.strftime('%Y-%m-%d (%a) %H:%M')} {preview.timezone}")
    st.caption(f"설정 파일: `{schedule_path}`")
