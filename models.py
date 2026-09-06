"""파이프라인 전역 데이터 모델."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Persona:
    name: str
    description: str
    daily_pain: str


@dataclass
class PainPoint:
    keyword: str
    problem: str
    relief: str


@dataclass
class ServiceAnalysis:
    service_name: str
    one_liner: str
    core_features: list[str]
    target_personas: list[Persona]
    pain_points: list[PainPoint]
    tone_notes: str = ""
    source_files: list[str] = field(default_factory=list)
    used_fallback: bool = False
    service_context: str = ""
    strategy_context: str = ""
    forbidden_words: list[str] = field(default_factory=list)
    marketing_angles: list[str] = field(default_factory=list)
    writing_style: str = ""


@dataclass
class ContentDraft:
    topic: str
    pain_point: str
    persona: str
    content: str
    keywords: list[str] = field(default_factory=list)


@dataclass
class PostRecord:
    id: int
    topic: str
    pain_point: str
    content: str
    status: str
    created_at: str
    posted_at: str | None = None


@dataclass
class PublishResult:
    success: bool
    post_url: str = ""
    comment_ok: bool = False
    error: str = ""
    screenshot_path: str = ""
