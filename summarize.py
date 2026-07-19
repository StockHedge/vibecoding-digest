#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
summarize.py — Gemini API로 각 글의 한글 번역 제목·요약, 전체 하이라이트 1건을 생성한다.

REST 호출(표준 라이브러리 urllib만 사용, SDK 미설치):
  POST https://generativelanguage.googleapis.com/v1beta/models/<MODEL>:generateContent
  인증: x-goog-api-key 헤더(URL 쿼리스트링에 키를 싣지 않는다 — 로그/프록시 노출 방지).

기본 모델 선정 근거(2026-07-19, GET /v1beta/models 실호출로 확인 — 이 호출이 키
검증도 겸한다):
  gemini-3.5-flash (version "3.5-flash-05-2026") — 조회된 flash 계열 중 이름에
  "preview"가 붙지 않은 가장 최신 GA 모델. 그 위의 gemini-3-flash-preview,
  gemini-3.1-flash-lite-preview 등은 preview 단계라 배제했다. env GEMINI_MODEL로
  override 가능(예: GA 승격 전 상위 preview로 실험하고 싶을 때).

호출 구조(2단계):
  1. 커뮤니티 단위 배치 호출 — 커뮤니티마다 1회, 그 커뮤니티의 글 전체(제목·url·
     fetch_content.py가 확보한 본문 발췌)를 프롬프트에 담아 각 글의 한글 번역
     제목·3~5문장 요약을 JSON(response_mime_type=application/json + responseSchema)
     으로 받는다. 호출 자체가 실패하면 그 커뮤니티의 모든 글을 "요약 실패"로
     표시하고 예외를 던지지 않는다(호출 실패 격리, 스펙 요구사항).
  2. 하이라이트 선정 — 1단계에서 요약에 성공한 글 전체(모든 커뮤니티 통틀어)를
     후보로 모아 별도 호출 1회로 "전체에서" 가장 주목할 만한 글 1건과 한 줄 사유를
     고른다. 이 호출까지 실패하면 규칙 기반 폴백(점수 최고 글)으로 대체한다 —
     하이라이트가 아예 없는 상태로 PDF·카카오 메시지가 만들어지는 것을 막기 위함.

실패 시 원제+링크만 남기고 "요약 실패"로 표시(글 단위), timeout 60s·재시도
2회(지수 백오프, 429/5xx만). API 키 값은 어떤 경우에도 로그에 남기지 않는다.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from typing import Optional

from kakao_sender import DEFAULT_ENV_PATH, get_env_value, load_env

logger = logging.getLogger("summarize")

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.5-flash"
ENV_API_KEY = "GEMINI_API_KEY"
ENV_MODEL = "GEMINI_MODEL"

TIMEOUT = 60.0
RETRIES = 2
BACKOFF_BASE = 2.0  # 초 (2s → 4s)

BATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "posts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "translated_title": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["index", "translated_title", "summary"],
            },
        },
    },
    "required": ["posts"],
}

HIGHLIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "community_id": {"type": "string"},
        "post_index": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["community_id", "post_index", "reason"],
}


# ── 설정 로드 ────────────────────────────────────────────────────────
def resolve_config(env_path: str = DEFAULT_ENV_PATH) -> tuple:
    """(model, api_key)를 반환. GEMINI_API_KEY 미설정 시 RuntimeError."""
    file_data = load_env(env_path)
    api_key = get_env_value(ENV_API_KEY, env_path, file_data)
    model = get_env_value(ENV_MODEL, env_path, file_data) or DEFAULT_MODEL
    if not api_key:
        raise RuntimeError(f"{ENV_API_KEY} 미설정 — .env를 확인하세요.")
    return model, api_key


def list_models(api_key: str, timeout: float = 30.0) -> list:
    """GET /v1beta/models — 가용 모델 목록 조회(키 검증 겸용)."""
    url = f"{GEMINI_API_BASE}/models"
    req = urllib.request.Request(url, headers={"x-goog-api-key": api_key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("models", [])


# ── REST 호출 ────────────────────────────────────────────────────────
def _gemini_generate(model: str, api_key: str, prompt: str, response_schema: dict,
                     timeout: float = TIMEOUT, retries: int = RETRIES) -> dict:
    """generateContent 1회 호출 + JSON 파싱. 일시적 오류(429/5xx/네트워크)만 재시도."""
    url = f"{GEMINI_API_BASE}/models/{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "responseSchema": response_schema,
        },
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}

    attempt = 0
    while True:
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            parsed = json.loads(raw)
            text = parsed["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            transient = e.code == 429 or 500 <= e.code < 600
            if not transient or attempt >= retries:
                snippet = err_body[:300] + ("…" if len(err_body) > 300 else "")
                raise RuntimeError(f"Gemini HTTP {e.code}: {snippet}") from e
            logger.warning("Gemini HTTP %s 일시 오류, 재시도 %d/%d", e.code, attempt + 1, retries)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt >= retries:
                raise RuntimeError(f"Gemini 네트워크 오류: {e}") from e
            logger.warning("Gemini 네트워크 오류(%s), 재시도 %d/%d", e, attempt + 1, retries)
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            # 응답 형식 이상은 재시도로 풀리지 않으므로 즉시 실패.
            raise RuntimeError(f"Gemini 응답 파싱 실패: {e}") from e
        attempt += 1
        time.sleep(BACKOFF_BASE * (2 ** (attempt - 1)))


# ── 커뮤니티 단위 배치 요약 ──────────────────────────────────────────
def _build_batch_prompt(community_name: str, posts: list) -> str:
    lines = [
        f"다음은 '{community_name}' 커뮤니티에서 수집한 글 목록이다.",
        "각 글에 대해 (1) 한국어로 번역한 제목(translated_title), (2) 본문 발췌를 바탕으로 한"
        " 한국어 요약 3~5문장(summary)을 작성하라.",
        "본문 발췌가 '(확보 실패)'로 표시된 글은 제목만으로 판단 가능한 한도에서 짧게 요약하라"
        "(추측으로 없는 내용을 지어내지 말 것).",
        "이미 한국어인 제목/본문은 자연스럽게 다듬기만 하고 억지로 재번역하지 말라.",
        "응답은 반드시 지정된 JSON 스키마로만 작성하고, 각 글의 index를 그대로 포함하라.",
        "",
    ]
    for i, p in enumerate(posts):
        lines.append(f"[index={i}] 제목: {p['title']}")
        lines.append(f"URL: {p['url']}")
        content = p.get("content")
        lines.append(f"본문 발췌: {content if content else '(확보 실패)'}")
        lines.append("")
    return "\n".join(lines)


def summarize_community(community_name: str, posts: list, model: str, api_key: str) -> None:
    """posts(title/url/content 보유)에 translated_title/summary/summary_failed를 채운다(in-place).

    호출 자체(네트워크·파싱 등)가 실패하면 이 커뮤니티의 모든 글을 요약 실패로
    표시하고 예외를 던지지 않는다.
    """
    if not posts:
        return
    prompt = _build_batch_prompt(community_name, posts)
    try:
        result = _gemini_generate(model, api_key, prompt, BATCH_SCHEMA)
        by_index = {
            item.get("index"): item
            for item in result.get("posts", [])
            if isinstance(item, dict)
        }
    except Exception as e:  # noqa: BLE001 - 커뮤니티 단위 요약 실패 격리(스펙 요구사항)
        logger.warning("%s: Gemini 요약 실패(%s) — 전체 글 요약 실패로 표시", community_name, e)
        for p in posts:
            p["translated_title"] = None
            p["summary"] = None
            p["summary_failed"] = True
        return

    for i, p in enumerate(posts):
        item = by_index.get(i)
        if item and item.get("translated_title") and item.get("summary"):
            p["translated_title"] = item["translated_title"]
            p["summary"] = item["summary"]
            p["summary_failed"] = False
        else:
            p["translated_title"] = None
            p["summary"] = None
            p["summary_failed"] = True

    ok = sum(1 for p in posts if not p["summary_failed"])
    logger.info("%s: 요약 %d/%d건 성공", community_name, ok, len(posts))


# ── 전체 하이라이트 선정 ─────────────────────────────────────────────
def _build_highlight_prompt(candidates: list) -> str:
    lines = [
        "다음은 이번 다이제스트에서 요약에 성공한 글 후보 목록이다.",
        "가장 주목할 만한 글 1건을 골라 community_id와 post_index를 후보 목록에 있는 값 그대로"
        " 반환하고, 왜 주목할 만한지 한국어 한 문장(reason)을 작성하라.",
        "",
    ]
    for c in candidates:
        lines.append(
            f"[community_id={c['community_id']} post_index={c['post_index']}] "
            f"({c['community_name']}) {c['translated_title']} — {c['summary'][:80]}"
        )
    return "\n".join(lines)


def pick_highlight(results: list, model: str, api_key: str) -> Optional[dict]:
    """요약 성공한 글 중 Gemini로 하이라이트 1건을 선정. 후보가 없으면 None.

    Gemini 호출이 실패하거나 응답이 후보와 매칭되지 않으면 규칙 기반(점수 최고 글)
    폴백을 사용한다 — 하이라이트 선정 단계의 일시적 오류가 다이제스트 발송 자체를
    막지 않도록 하기 위함.
    """
    candidates = []
    for r in results:
        if r.get("error"):
            continue
        for i, p in enumerate(r["posts"]):
            if not p.get("summary_failed") and p.get("summary"):
                candidates.append({
                    "community_id": r["community_id"],
                    "community_name": r["community_name"],
                    "post_index": i,
                    "translated_title": p["translated_title"],
                    "summary": p["summary"],
                    "url": p["url"],
                    "score": p.get("score", 0),
                })
    if not candidates:
        return None

    try:
        prompt = _build_highlight_prompt(candidates)
        result = _gemini_generate(model, api_key, prompt, HIGHLIGHT_SCHEMA)
        cid, idx = result.get("community_id"), result.get("post_index")
        match = next(
            (c for c in candidates if c["community_id"] == cid and c["post_index"] == idx),
            None,
        )
        if match:
            return {**match, "reason": result.get("reason") or ""}
        logger.warning("하이라이트 응답이 후보와 매칭되지 않음 — 규칙 기반 폴백 사용")
    except Exception as e:  # noqa: BLE001 - 하이라이트 선정 실패는 규칙 기반으로 폴백
        logger.warning("하이라이트 Gemini 선정 실패(%s) — 규칙 기반 폴백 사용", e)

    best = max(candidates, key=lambda c: c.get("score", 0))
    return {**best, "reason": "이번 다이제스트에서 가장 높은 반응(추천/포인트)을 얻은 글"}


# ── 전체 오케스트레이션 ──────────────────────────────────────────────
def summarize_all(results: list, env_path: str = DEFAULT_ENV_PATH) -> Optional[dict]:
    """모든 커뮤니티 결과에 배치 요약을 채우고, 전체 하이라이트 1건을 반환한다.

    GEMINI_API_KEY 자체가 없으면 전체 글을 요약 실패로 표시하고 None을 반환한다
    (파이프라인은 계속 진행 — PDF는 원제+링크만으로 생성됨).
    """
    try:
        model, api_key = resolve_config(env_path)
    except Exception as e:  # noqa: BLE001 - 설정 부재는 전체 요약 실패로 취급하고 계속 진행
        logger.warning("Gemini 설정 없음(%s) — 전체 글을 요약 실패로 표시", e)
        for r in results:
            if r.get("error"):
                continue
            for p in r["posts"]:
                p["translated_title"] = None
                p["summary"] = None
                p["summary_failed"] = True
        return None

    for r in results:
        if r.get("error") or not r.get("posts"):
            continue
        summarize_community(r["community_name"], r["posts"], model, api_key)

    return pick_highlight(results, model, api_key)


# ── CLI (검증용) ─────────────────────────────────────────────────────
def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Gemini 요약 모듈")
    parser.add_argument("--env", default=DEFAULT_ENV_PATH, help=".env 경로 (기본 .env)")
    parser.add_argument("--check", action="store_true",
                        help="모델 목록 조회 + 선정된 기본 모델 확인(요약 실행 안 함)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.check:
        try:
            model, api_key = resolve_config(args.env)
            models = list_models(api_key, timeout=30.0)
        except Exception as e:  # noqa: BLE001 - CLI 최상위 경계
            logger.error("확인 실패: %s", e)
            return 1
        flash_models = sorted(
            m.get("name", "") for m in models if "flash" in m.get("name", "").lower()
        )
        print(f"조회된 flash 계열 모델 수: {len(flash_models)}")
        for name in flash_models:
            print(f"  - {name}")
        print(f"선정된 기본 모델(GEMINI_MODEL 미설정 시): {model}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
