"""docs/service.md와 docs/strategy.md를 분리 로드한 뒤 LLM으로 소구점 매트릭스를 추출한다."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import config
from models import PainPoint, Persona, ServiceAnalysis
from services.llm import generate_json

logger = logging.getLogger(__name__)

MAX_DOC_CHARS = 40_000
SERVICE_FILENAMES = ("service.md", "service.txt")
STRATEGY_FILENAMES = ("strategy.md", "strategy.txt")
PAIN_FILENAMES = ("pain_points.md", "pain_points.txt")

FALLBACK_SERVICE_DOC = """
# FocusFlow — 서비스 스펙 (기본값)

FocusFlow는 사이드 프로젝트를 밤에만 하는 1인 개발자가
'오늘은 진짜 한 시간만'을 지키도록 돕는 미니멀 집중 타이머입니다.

## 핵심 기능
- 25/50분 집중 세션과 짧은 휴식 알림
- 방해 앱/사이트 차단 목록
- 세션이 끝날 때마다 한 줄 회고 기록
- 주간 집중 시간 리포트 (과한 대시보드 없이)

## 해소하는 불편
- 저녁에 노트북은 열었는데 SNS만 보다가 하루가 끝남
- 거창한 칸반/노트 앱을 세팅하다 정작 코딩을 못 함
- 며칠 쉬면 다시 시작하기가 무서워짐
- 내가 얼마나 꾸준한지 숫자로 안 보여 동기가 떨어짐
""".strip()

FALLBACK_STRATEGY_DOC = """
# FocusFlow — 마케팅 전략 (기본값)

## 타겟 페르소나
- 야간 사이드잡 개발자: 본업 후 밤에만 사이드 프로젝트를 하는 직장인. 노트북은 열었지만 SNS만 보다가 하루가 끝난다.
- 도구에 지친 솔로 메이커: 생산성 앱 세팅에 시간을 쓰는 1인 개발자. 칸반을 꾸미다 정작 제품 코드를 못 짠다.

## 글쓰기 톤앤매너
- 1인 개발자가 친구에게 개발 일기를 보내듯 담백한 반말/구어체
- 과장 광고, 허위 후기, 보장형 문구 금지
- 본문에 외부 링크·가입 CTA를 넣지 않는다 (댓글 분리)

## 마케팅 앵글
- 공감형: 잠재 고객의 일상적 고통을 먼저 보여 준다
- 개발 비하인드: 왜 그 기능을 직접 만들었는지 작은 과정을 보여 준다

## 금지어
인생이 바뀝니다, 무조건, 보장, 대박, 지금 가입, 한정 수량, 100%, 필수템, 혁명
""".strip()

MEMBER_GENERIC_STRATEGY = """
# 멤버용 내부 글쓰기 가이드

이 문서는 화면에 보여주지 않는다. 서비스 문서와 고충 인벤토리만 고객 글의 재료다.

## 글쓰기 톤앤매너
- 잠재 고객에게 친구처럼 말하는 담백한 구어체
- 한 글에 고충 1개만 사용
- 본문에 앱 이름 반복, 가입 CTA, 외부 링크를 넣지 않는다
- 과장 광고, 허위 후기, 보장형 문구 금지

## 금지어
인생이 바뀝니다, 무조건, 보장, 대박, 지금 가입, 한정 수량, 100%, 필수템, 혁명
""".strip()

FALLBACK_PAIN_DOC = """
# 고충 인벤토리 (기본값)

## P01 전화 확인
- **키워드:** 전화 확인
- **장면:** 카페마다 전화해서 강아지 가능한지 묻는다.
""".strip()

_cache_key: str | None = None
_cache_value: ServiceAnalysis | None = None


@dataclass
class LoadedDocs:
    service_text: str
    strategy_text: str
    source_files: list[str]
    service_fallback: bool
    strategy_fallback: bool

    @property
    def used_fallback(self) -> bool:
        return self.service_fallback or self.strategy_fallback

    @property
    def fingerprint(self) -> str:
        blob = f"{self.service_text}\n---\n{self.strategy_text}"
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _clip(text: str) -> str:
    if len(text) <= MAX_DOC_CHARS:
        return text
    return text[:MAX_DOC_CHARS] + "\n\n[문서가 길어 일부가 생략되었습니다]"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="cp949", errors="replace")


def _load_named_doc(
    docs_dir: Path,
    names: tuple[str, ...],
    fallback: str,
    label: str,
) -> tuple[str, str, bool]:
    """지정 파일명을 읽고, 없거나 비어 있으면 fallback을 반환한다."""
    docs_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = docs_dir / name
        if not path.is_file():
            continue
        text = _read_text(path).strip()
        logger.info("%s 문서 로드: %s (%d chars)", label, path.name, len(text))
        return _clip(text), path.name, False

    tried = ", ".join(names)
    logger.warning("%s 문서가 없거나 비어 있어 기본 템플릿을 사용합니다. (탐색: %s)", label, tried)
    return fallback, f"(fallback:{names[0]})", True


def load_documents(
    docs_dir: Path | None = None,
    *,
    use_generic_strategy: bool = False,
) -> LoadedDocs:
    """service.md, strategy.md, pain_points.md를 로드한다. 고충 목록은 전략 컨텍스트에 합친다."""
    directory = docs_dir or config.DOCS_DIR
    service_text, service_name, service_fallback = _load_named_doc(
        directory, SERVICE_FILENAMES, FALLBACK_SERVICE_DOC, "서비스"
    )
    if use_generic_strategy:
        strategy_text, strategy_name, strategy_fallback = (
            MEMBER_GENERIC_STRATEGY,
            "(internal:member-strategy)",
            True,
        )
    else:
        strategy_text, strategy_name, strategy_fallback = _load_named_doc(
            directory, STRATEGY_FILENAMES, FALLBACK_STRATEGY_DOC, "전략"
        )
    pain_text, pain_name, pain_fallback = _load_named_doc(
        directory, PAIN_FILENAMES, FALLBACK_PAIN_DOC, "고충"
    )
    sources = [service_name, strategy_name]
    merged_strategy = strategy_text
    if pain_text:
        merged_strategy = (
            f"{strategy_text}\n\n---\n\n# 고충 인벤토리 (하나씩만 사용)\n{pain_text}"
        )
        sources.append(pain_name)
    return LoadedDocs(
        service_text=service_text,
        strategy_text=merged_strategy,
        source_files=sources,
        service_fallback=service_fallback,
        strategy_fallback=strategy_fallback and pain_fallback,
    )


def clear_analysis_cache() -> None:
    global _cache_key, _cache_value
    _cache_key = None
    _cache_value = None


def generate_pain_points_markdown(service_text: str) -> str:
    """서비스 문서를 분석해 pain_points.md 형식의 고충 인벤토리를 만든다."""
    text = (service_text or "").strip()
    if not text:
        raise ValueError("서비스 문서가 비어 있어 고충을 만들 수 없습니다.")

    language = "Korean" if config.LANGUAGE.startswith("ko") else "English"
    system = (
        "You extract a customer pain-point inventory from a service specification. "
        "Return JSON only. Do not invent pains that are unrelated to the service. "
        "Prefer pains the product actually relieves. "
        f"Write all strings in {language}."
    )
    user = f"""
아래 서비스 문서를 읽고, 고객이 서비스를 쓰기 전에 겪는 일상 고충을 8~12개 뽑으세요.
문서에 '해결하는 문제'가 있으면 그것을 우선하고, 없으면 기능으로부터 추론하세요.
한 항목은 한 장면만. 앱 가입 CTA나 기능 나열은 넣지 마세요.

[서비스 문서]
{text[:20000]}

스키마:
{{
  "pain_points": [
    {{
      "keyword": "짧은 키워드",
      "scene": "구체적인 일상 장면 2~4문장",
      "hook": "한 줄 훅",
      "question": "공감을 묻는 짧은 질문"
    }}
  ]
}}
""".strip()
    data = generate_json(system, user, temperature=0.4)
    items = data.get("pain_points") or []
    if not isinstance(items, list) or not items:
        raise RuntimeError("고충 목록을 만들지 못했습니다.")

    lines = [
        "# 고충 인벤토리",
        "",
        "한 글에 고충 **1개만** 꺼낸다. 최근 7일 안에 쓴 키워드는 다시 쓰지 않는다.",
        "앱 이름·가입 CTA·기능 나열은 본문에 넣지 않는다.",
        "",
        "---",
        "",
    ]
    count = 0
    for raw in items:
        if not isinstance(raw, dict):
            continue
        keyword = str(raw.get("keyword") or "").strip()
        scene = str(raw.get("scene") or "").strip()
        hook = str(raw.get("hook") or "").strip()
        question = str(raw.get("question") or "").strip()
        if not keyword:
            continue
        count += 1
        lines.append(f"## P{count:02d} {keyword}")
        lines.append(f"- **키워드:** {keyword}")
        if scene:
            lines.append(f"- **장면:** {scene}")
        if hook:
            lines.append(f"- **훅 씨앗:** {hook}")
        if question:
            lines.append(f"- **공감 질문:** {question}")
        lines.append("")
    if count == 0:
        raise RuntimeError("고충 항목이 비어 있습니다.")
    return "\n".join(lines).strip() + "\n"


def analyze_or_fallback(
    docs_dir: Path | None = None,
    *,
    use_generic_strategy: bool = False,
) -> ServiceAnalysis:
    """LLM 분석이 실패해도 로컬 문서로 초안 생성은 이어가게 한다."""
    try:
        return analyze_documents(
            docs_dir=docs_dir,
            use_generic_strategy=use_generic_strategy,
        )
    except Exception:
        logger.exception("문서 LLM 분석 실패, 로컬 문서로 fallback 합니다.")
        return _fallback_analysis(
            load_documents(docs_dir, use_generic_strategy=use_generic_strategy)
        )


def analyze_documents(
    force: bool = False,
    docs_dir: Path | None = None,
    *,
    use_generic_strategy: bool = False,
) -> ServiceAnalysis:
    """두 문서를 분석해 기능/페르소나/소구점 매트릭스를 반환. 동일 문서는 메모리 캐시."""
    global _cache_key, _cache_value

    loaded = load_documents(docs_dir, use_generic_strategy=use_generic_strategy)
    cache_key = f"{docs_dir or ''}:{int(use_generic_strategy)}:{loaded.fingerprint}"
    if not force and _cache_value is not None and _cache_key == cache_key:
        logger.info("문서 분석 캐시 사용 (%s)", ", ".join(loaded.source_files))
        return _cache_value

    if not config.GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY가 없어 문서 fallback 분석 결과를 사용합니다.")
        analysis = _fallback_analysis(loaded)
        _cache_key, _cache_value = cache_key, analysis
        return analysis

    language = "Korean" if config.LANGUAGE.startswith("ko") else "English"

    system = (
        "You extract a marketing insight matrix from a service spec, a strategy doc, "
        "and a stored pain-point inventory. Return JSON only. "
        "Do not invent features that are absent from the service doc. "
        "Personas, tone, forbidden words, and marketing angles MUST come from the strategy doc. "
        "pain_points MUST be copied from the 고충 인벤토리 (keyword + 장면 as problem). "
        "Do not invent extra pains. Do not use 'developer marketing burnout' as a persona. "
        f"Write all user-facing strings in {language}."
    )
    user = f"""
아래 문서를 구분해 분석하고 JSON으로 답하세요.

[서비스 문서] — 스펙/기능. 여기서만 기능을 추출하세요.
데이터 근간이 2026년 블로그라면 그 사실을 core/one_liner에 왜곡 없이 반영하세요.
{loaded.service_text}

[전략 문서 + 고충 인벤토리] — 페르소나, 톤, 금지어, 앵글, 저장된 고충.
{loaded.strategy_text}

스키마:
{{
  "service_name": "string",
  "one_liner": "한 줄 소개",
  "core_features": ["기능1", "기능2"],
  "target_personas": [
    {{"name": "페르소나 이름", "description": "누구인지", "daily_pain": "일상에서 겪는 고통"}}
  ],
  "pain_points": [
    {{"keyword": "인벤토리 키워드 그대로", "problem": "장면", "relief": "이 고충을 어떻게 덜어주는지(과장 없이)"}}
  ],
  "tone_notes": "전략 문서의 톤앤매너 요약",
  "writing_style": "문체 지시",
  "forbidden_words": ["금지어1"],
  "marketing_angles": ["공감형", "최신성 공감"]
}}

규칙:
- 핵심 기능은 서비스 문서에 있는 것만 3~6개. 장소 데이터의 근간이 2026년 블로그라면 그 사실을 빠뜨리지 말 것
- 페르소나는 전략 문서의 보호자만. 1인 개발자 마케팅 페르소나 금지
- pain_points는 고충 인벤토리 항목을 모두 옮긴다 (키워드 유지)
- 한 글에서 고충을 여러 개 섞으라는 지시가 아님. 목록만 추출
- 금지어·앵글을 전략에서 빠짐없이 옮길 것
""".strip()

    logger.info(
        "문서 분석 LLM 호출 (service=%d chars, strategy=%d chars, files=%s)",
        len(loaded.service_text),
        len(loaded.strategy_text),
        loaded.source_files,
    )
    data = generate_json(system, user, temperature=0.3)
    analysis = _parse_analysis(data, loaded)
    _cache_key, _cache_value = cache_key, analysis
    logger.info(
        "분석 완료: %s / 기능 %d / 페르소나 %d / 소구점 %d / 앵글 %s / 금지어 %d",
        analysis.service_name,
        len(analysis.core_features),
        len(analysis.target_personas),
        len(analysis.pain_points),
        analysis.marketing_angles,
        len(analysis.forbidden_words),
    )
    return analysis


def _parse_pain_inventory(text: str) -> list[PainPoint]:
    """pain_points.md 형식에서 키워드·장면을 뽑아 저장한다."""
    points: list[PainPoint] = []
    current_kw = ""
    current_problem = ""
    current_relief = ""

    def flush() -> None:
        nonlocal current_kw, current_problem, current_relief
        if current_kw:
            points.append(
                PainPoint(
                    keyword=current_kw,
                    problem=current_problem or current_kw,
                    relief=current_relief,
                )
            )
        current_kw = ""
        current_problem = ""
        current_relief = ""

    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("## P") and current_kw:
            flush()
        if line.startswith("- **키워드:**"):
            current_kw = line.split(":**", 1)[-1].strip()
        elif line.startswith("- **장면:**"):
            current_problem = line.split(":**", 1)[-1].strip()
        elif line.startswith("- **훅 씨앗:**") and not current_relief:
            current_relief = line.split(":**", 1)[-1].strip()
    flush()
    return points


def _parse_analysis(data: dict, loaded: LoadedDocs) -> ServiceAnalysis:
    fallback = _fallback_analysis(loaded)

    personas = [
        Persona(
            name=str(item.get("name", "사용자")).strip() or "사용자",
            description=str(item.get("description", "")).strip(),
            daily_pain=str(item.get("daily_pain", "")).strip(),
        )
        for item in data.get("target_personas") or []
        if isinstance(item, dict)
    ]
    pain_points = [
        PainPoint(
            keyword=str(item.get("keyword", "")).strip(),
            problem=str(item.get("problem", "")).strip(),
            relief=str(item.get("relief", "")).strip(),
        )
        for item in data.get("pain_points") or []
        if isinstance(item, dict) and str(item.get("keyword", "")).strip()
    ]
    features = [str(x).strip() for x in (data.get("core_features") or []) if str(x).strip()]
    forbidden = [str(x).strip() for x in (data.get("forbidden_words") or []) if str(x).strip()]
    angles = [str(x).strip() for x in (data.get("marketing_angles") or []) if str(x).strip()]
    inventory = _parse_pain_inventory(loaded.strategy_text)
    if inventory:
        pain_points = inventory

    return ServiceAnalysis(
        service_name=str(data.get("service_name") or config.SERVICE_NAME or fallback.service_name).strip(),
        one_liner=str(data.get("one_liner") or fallback.one_liner).strip(),
        core_features=features or fallback.core_features,
        target_personas=personas or fallback.target_personas,
        pain_points=pain_points or fallback.pain_points,
        tone_notes=str(data.get("tone_notes") or fallback.tone_notes).strip(),
        writing_style=str(data.get("writing_style") or fallback.writing_style).strip(),
        source_files=loaded.source_files,
        used_fallback=loaded.used_fallback,
        service_context=loaded.service_text,
        strategy_context=loaded.strategy_text,
        forbidden_words=forbidden or fallback.forbidden_words,
        marketing_angles=angles or fallback.marketing_angles,
    )


def _fallback_analysis(loaded: LoadedDocs) -> ServiceAnalysis:
    inventory = _parse_pain_inventory(loaded.strategy_text)
    return ServiceAnalysis(
        service_name=config.SERVICE_NAME or "발도장 꾹꾹",
        one_liner="2026년 블로그에서 찾은 반려견 동반 장소를 지도에 모아, 전화·검색 없이 후보를 고르게 돕는 앱",
        core_features=[
            "2026년 발행 블로그 기반 동반 장소 지도",
            "반려견 체급 필터",
            "EXIF 검증 발도장",
            "장소 앰버서더",
        ],
        target_personas=[
            Persona(
                name="외출 장소를 못 정하는 보호자",
                description="주말에 강아지와 카페·식당을 가고 싶은 사람",
                daily_pain="검색해도 확신이 없어 전화하거나 동행을 기다리게 한다",
            ),
            Persona(
                name="동행이 있는 보호자",
                description="친구·가족과 만나는데 장소가 안 정해진 사람",
                daily_pain="나만 지도와 블로그를 오가며 나머지를 기다리게 한다",
            ),
        ],
        pain_points=inventory
        or [
            PainPoint("전화 확인", "갈 곳마다 전화해서 동반 가능 여부를 묻는다", "올해 블로그 후보를 지도에서 먼저 본다"),
            PainPoint("동행 대기", "동행은 나와 있는데 나만 가게를 못 정한다", "출발 전에 후보를 줄인다"),
        ],
        tone_notes="보호자 단톡 반말. 고충 1개만. 개발 마케팅 토로 금지.",
        writing_style="친구에게 '여기 전화해봤어?' 하듯 담백한 반말",
        source_files=loaded.source_files,
        used_fallback=True,
        service_context=loaded.service_text,
        strategy_context=loaded.strategy_text,
        forbidden_words=[
            "인생이 바뀝니다",
            "무조건",
            "보장",
            "대박",
            "지금 가입",
            "100%",
            "애견 동반 100%",
        ],
        marketing_angles=["공감형", "최신성 공감", "현장 거절형"],
    )
