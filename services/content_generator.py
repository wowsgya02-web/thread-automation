"""Threads 특화 마케팅 본문 생성. 서비스 스펙 + 마케팅 전략을 함께 반영한다."""

from __future__ import annotations

import json
import logging
import re

import config
from models import ContentDraft, ServiceAnalysis
from services.llm import generate_json

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https?://|www\.|\b[\w.-]+\.(com|net|io|kr|app)\b", re.I)
MAX_ATTEMPTS = 3
DEFAULT_FORBIDDEN = ("지금 가입", "링크는 댓글", "link in bio", "100%", "보장합니다")


def _is_awareness_phase(phase: str = "", extra_instructions: str = "") -> bool:
    """W-4 / Awareness: 고충 장면만. 이후 주차면 기능·서비스 스펙을 연다."""
    blob = f"{phase} {extra_instructions}".upper()
    later = ("W-3", "W-2", "W-1", "D-DAY", "D-DAY:", "W+", "INTEREST", "WAITLIST", "PRE-LAUNCH")
    if any(token in blob for token in later):
        return False
    return True


def generate_post(
    analysis: ServiceAnalysis,
    recent_topics: list[str] | None = None,
    blocked_terms: list[str] | None = None,
    extra_instructions: str = "",
    phase: str = "",
) -> ContentDraft:
    """고충 1개로 3~5줄 본문을 만든다. W-4는 기능 스펙을 넣지 않는다."""
    recent_topics = recent_topics or []
    blocked_terms = blocked_terms or []
    last_error = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        draft = _call_llm(
            analysis,
            recent_topics,
            blocked_terms,
            extra_instructions,
            retry_hint=last_error,
            phase=phase,
        )
        violation = _validate(draft, blocked_terms, analysis.forbidden_words)
        if not violation:
            logger.info("본문 생성 완료 (시도 %d): topic=%s", attempt, draft.topic)
            return draft
        last_error = violation
        logger.warning("생성 결과 거부 (시도 %d/%d): %s", attempt, MAX_ATTEMPTS, violation)
        blocked_terms = list(blocked_terms) + [draft.topic, draft.pain_point]

    raise RuntimeError(f"중복/규칙 위반 없는 본문을 만들지 못했습니다: {last_error}")


def rewrite_for_threads(
    source_text: str,
    analysis: ServiceAnalysis | None = None,
    extra_instructions: str = "",
    phase: str = "",
) -> ContentDraft:
    """사용자가 넣은 초안을 Threads 말투·훅·인기글 구조로 다시 쓴다. 사실관계는 유지."""
    source = (source_text or "").strip()
    if not source:
        raise RuntimeError("바꿀 글을 먼저 입력하세요.")

    language_line = (
        "Write the post body in natural Korean (반말/구어체, 친구에게 DM 보내듯)."
        if config.LANGUAGE.startswith("ko")
        else "Write the post body in natural, casual English."
    )
    forbidden = list(analysis.forbidden_words) if analysis else []
    system = f"""
You rewrite a user's draft into a high-performing Meta Threads post.
{language_line}

Goal: keep the user's facts and intent, but change voice and structure so it can stop the scroll.

Popular Threads structure (must follow, in this order):
1) Hook — first line only. Specific situation, sharp question, or a small concrete number. Not a slogan, not a summary.
2) Relatable beat — one everyday scene the reader nods at. Short lines.
3) Turn — a behind-the-scenes detail, a small confession, or one useful insight. Not a feature list.
4) Residual close — quiet ending. No hard sell.

Voice:
- Pet-owner talking to another pet-owner. Dry, specific, like a group chat.
- If strategy context exists, obey it (empathy first, one pain, 2026-blog recency only as feeling not a tech pitch).
- 3 to 5 short lines with line breaks. One idea only.
- NEVER add URLs, 'link in comments', '지금 가입', discounts, or fake testimonials.
- Do not invent features, numbers, or claims that are not in the source.
- Hashtags: none, or at most one quiet tag.
- Do not mention AI.
- If awareness_only: do not add app name, features, waitlist, or signup. Empathy scene only.

If a strategy/tone context is provided, obey its persona distance, forbidden words, and writing style.
""".strip()

    user = {
        "source_draft": source,
        "strategy_context": (analysis.strategy_context if analysis else None),
        "writing_style": (analysis.writing_style if analysis else None),
        "tone_notes": (analysis.tone_notes if analysis else None),
        "forbidden_words": forbidden or None,
        "phase": phase or None,
        "awareness_only": _is_awareness_phase(phase, extra_instructions),
        "extra_instructions": extra_instructions or None,
        "output_schema": {
            "topic": "짧은 주제 라벨",
            "pain_point": "이 글이 건드린 불편 키워드",
            "persona": "누구 시점으로 말했는지",
            "angle": "훅 유형 (질문/장면/숫자 등)",
            "content": "3~5줄 Threads 본문",
            "keywords": ["키워드"],
        },
    }
    data = generate_json(system, json.dumps(user, ensure_ascii=False, indent=2), temperature=0.8)
    draft = ContentDraft(
        topic=str(data.get("topic") or "rewrite").strip(),
        pain_point=str(data.get("pain_point") or "").strip(),
        persona=str(data.get("persona") or "").strip(),
        content=_strip_links(str(data.get("content") or "").strip()),
        keywords=[str(x).strip() for x in (data.get("keywords") or []) if str(x).strip()],
    )
    angle = str(data.get("angle") or "").strip()
    if angle and angle not in draft.keywords:
        draft.keywords.append(angle)

    violation = _validate(draft, blocked_terms=[], forbidden_words=forbidden)
    if violation:
        raise RuntimeError(f"리라이트 결과가 규칙을 어겼습니다: {violation}")
    return draft


def _call_llm(
    analysis: ServiceAnalysis,
    recent_topics: list[str],
    blocked_terms: list[str],
    extra_instructions: str,
    retry_hint: str,
    phase: str = "",
) -> ContentDraft:
    language_line = (
        "Write the post body in natural Korean (반말/구어체)."
        if config.LANGUAGE.startswith("ko")
        else "Write the post body in natural, casual English."
    )
    angles = analysis.marketing_angles or ["공감형", "최신성 공감"]
    awareness = _is_awareness_phase(phase, extra_instructions)
    phase_rule = (
        "- 지금 단계는 W-4 Awareness다. 서비스명, 기능, 지도 데모, 가입/대기 유도 금지. "
        "고충 인벤토리의 장면 1개만 쓰고 공감 질문으로 끝낸다."
        if awareness
        else "- 고충 1개로 시작한 뒤에만, 서비스 스펙에서 그 고충을 덜어 주는 장면 한 줄을 넣을 수 있다."
    )
    system = f"""
You are a copywriter for a pet-friendly place map app's Threads account.
{language_line}

You receive strategy and a STORED pain inventory as the source of truth.
{phase_rule}

[전략·고충 인벤토리] 최우선:
- 화자는 반려견과 식당/카페를 가려는 보호자다. 1인 개발자 마케팅 토로 금지.
- 이번 글은 인벤토리 고충을 딱 1개만 고른다. 전화+동행대기를 한 글에 섞지 말 것.
- 첫 문장은 그 고충의 장면 훅. 슬로건 금지.
- 금지어 준수. 기능 나열·브로슈어 톤 금지.
- 최근 사용한 키워드는 피하고, extra_instructions의 '아직 안 쓴 소구점'을 우선한다.

[데이터 근간]:
- 지도 장소의 출발점은 2026년에 발행된 블로그에서 반려견 가능 장소를 찾아 표시한 것이다.
- 최신성은 "철 지난 후기를 못 믿겠다"는 감정으로만 풀 수 있다. 크롤링/봇/파이프라인을 설명하지 말 것.
- 입장 100%, 공식 허가 명단이라고 쓰지 말 것.

공통:
- 3~5줄. 줄바꿈. 본문 URL·가입 CTA·'링크는 댓글에' 금지.
- 해시태그 없거나 1개. AI 티 내지 말 것.
""".strip()

    extracted_service: dict = {
        "service_name": analysis.service_name,
        "one_liner": analysis.one_liner,
    }
    if not awareness:
        extracted_service["core_features"] = analysis.core_features
        extracted_service["pain_points"] = [p.__dict__ for p in analysis.pain_points]
    else:
        extracted_service["pain_points"] = [p.__dict__ for p in analysis.pain_points]

    user = {
        "phase": phase or ("W-4 Awareness" if awareness else None),
        "service_context": None if awareness else analysis.service_context,
        "strategy_context": analysis.strategy_context,
        "extracted_service": extracted_service,
        "extracted_strategy": {
            "target_personas": [p.__dict__ for p in analysis.target_personas],
            "tone_notes": analysis.tone_notes,
            "writing_style": analysis.writing_style,
            "forbidden_words": analysis.forbidden_words,
            "marketing_angles": angles,
        },
        "recent_topics_do_not_repeat": recent_topics,
        "blocked_keywords_do_not_repeat": blocked_terms,
        "extra_instructions": extra_instructions or None,
        "retry_hint": retry_hint or None,
        "output_schema": {
            "topic": "짧은 주제 라벨",
            "pain_point": "이번에 고른 소구점 키워드 1개",
            "persona": "전략 문서의 페르소나 이름",
            "angle": "전략 문서의 마케팅 앵글 중 하나",
            "content": "3~5줄 본문 (줄바꿈 포함, 기능 나열 금지)",
            "keywords": ["중복 체크용 키워드들"],
        },
    }
    data = generate_json(system, json.dumps(user, ensure_ascii=False, indent=2), temperature=0.85)
    content = _strip_links(str(data.get("content") or "").strip())
    keywords = [str(x).strip() for x in (data.get("keywords") or []) if str(x).strip()]
    angle = str(data.get("angle") or "").strip()
    if angle and angle not in keywords:
        keywords.append(angle)
    return ContentDraft(
        topic=str(data.get("topic") or "untitled").strip(),
        pain_point=str(data.get("pain_point") or "").strip(),
        persona=str(data.get("persona") or "").strip(),
        content=content,
        keywords=keywords,
    )


def _strip_links(text: str) -> str:
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        if URL_PATTERN.search(line):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def _validate(
    draft: ContentDraft,
    blocked_terms: list[str],
    forbidden_words: list[str] | None = None,
) -> str:
    if not draft.content:
        return "본문이 비어 있습니다."
    line_count = len([ln for ln in draft.content.splitlines() if ln.strip()])
    if line_count < 3:
        return "본문이 3줄 미만입니다."
    if line_count > 8:
        return "본문이 너무 깁니다."
    if URL_PATTERN.search(draft.content):
        return "본문에 링크성 텍스트가 포함되어 있습니다."

    haystack = f"{draft.content}\n{draft.topic}".lower()
    banned = list(DEFAULT_FORBIDDEN) + list(forbidden_words or [])
    for phrase in banned:
        token = phrase.strip()
        if token and token.lower() in haystack:
            return f"금지 표현 사용: {phrase}"

    for term in blocked_terms:
        if not term:
            continue
        if term.lower() in (draft.topic.lower(), draft.pain_point.lower()):
            return f"최근 사용한 주제/소구점과 겹칩니다: {term}"
    return ""
