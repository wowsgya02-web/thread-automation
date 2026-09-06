"""Gemini API 호출 래퍼."""

from __future__ import annotations

import json
import logging
import re

from google import genai
from google.genai import types

import config

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I | re.M)
_CLIENT: genai.Client | None = None
_CLIENT_KEY: str | None = None


def _client() -> genai.Client:
    """httpx 세션이 GC로 닫히지 않도록 프로세스 동안 클라이언트를 재사용한다."""
    global _CLIENT, _CLIENT_KEY
    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY가 없습니다. .env에 Google AI Studio 키를 넣으세요."
        )
    if _CLIENT is None or _CLIENT_KEY != config.GEMINI_API_KEY:
        _CLIENT = genai.Client(api_key=config.GEMINI_API_KEY)
        _CLIENT_KEY = config.GEMINI_API_KEY
    return _CLIENT


def generate_json(
    system_instruction: str,
    user_content: str,
    temperature: float = 0.3,
) -> dict:
    """시스템+유저 프롬프트로 JSON 객체를 생성한다."""
    client = _client()
    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
            response_mime_type="application/json",
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    raw = (response.text or "").strip()
    if not raw:
        raise RuntimeError("Gemini 응답이 비어 있습니다.")
    raw = _FENCE_RE.sub("", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Gemini JSON 파싱 실패: %s", raw[:500])
        raise RuntimeError("Gemini 응답이 JSON이 아닙니다.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Gemini 응답이 JSON 객체가 아닙니다.")
    return data
