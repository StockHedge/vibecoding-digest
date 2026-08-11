#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hub_sender.py — 다이제스트를 for-marketing 허브로 push(IG 카드뉴스 원재료).

계약: for-marketing 저장소 `docs/contracts/t9-integration.md` §6.
  POST {HUB_BASE_URL}/api/ingest/vibecoding/digest
  헤더 X-Hub-Timestamp / X-Hub-Signature = hex(hmac_sha256(secret, f"{ts}." + body))

설계 원칙 두 가지:

1. **fail-soft.** 이 루틴의 본래 목적은 카카오톡 발송이다. 허브가 죽었거나 시크릿이
   없다고 해서 다이제스트 발송 자체가 실패하면 안 된다. 모든 실패를 dict로 돌려주고
   예외를 던지지 않는다(kakao_sender·gdrive·publish와 같은 관례).
2. **서명한 bytes를 그대로 보낸다.** 한글이 포함되므로 ensure_ascii=False로 직렬화한
   UTF-8 bytes 하나를 만들어 서명과 전송에 동일하게 쓴다. 재직렬화하면 공백·키 순서
   차이로 서명이 깨진다.

표준 라이브러리만 사용한다(이 저장소 관례 — requirements.txt는 fpdf2 하나뿐).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from kakao_sender import DEFAULT_ENV_PATH, get_env_value, load_env

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
ENDPOINT_PATH = "/api/ingest/vibecoding/digest"
TIMEOUT_S = 20.0


def build_payload(results: list, window_start: datetime, window_end: datetime) -> dict:
    """collect_all 결과 + summarize 결과를 계약 §6 스키마로 변환한다.

    `digest_id`는 수집 창 종료일의 KST 날짜다 — 3일 주기라 하루에 두 번 돌 일이 없고,
    사람이 보고 어느 회차인지 바로 아는 값이어야 재전송·디버깅이 쉽다.

    수집 실패한 커뮤니티(error 보유)는 제외한다. 요약 실패 글(summary=None)은 그대로
    보낸다 — 버릴지는 허브의 카드 생성 단계가 정한다(수집기가 편집 정책을 알 필요 없다).
    """
    communities = []
    for r in results:
        if r.get("error"):
            continue
        posts = [
            {
                "title": p.get("title") or "",
                "url": p.get("url") or "",
                "translated_title": p.get("translated_title"),
                "summary": p.get("summary"),
                "score": p.get("score"),
            }
            for p in r.get("posts", [])
            if p.get("url")
        ]
        if not posts:
            continue
        # 키가 바뀌면 조용히 빈 출처로 나가는 대신 로그로 드러낸다.
        if not (r.get("community_name") or r.get("community_id")):
            logger.warning(
                "커뮤니티 식별자를 찾을 수 없다 — collect 결과 구조 변경 의심: keys=%s",
                sorted(r)[:6],
            )
        # collect_community()가 돌려주는 키는 community_id / community_name 이다.
        # id/name 으로 읽으면 조용히 빈 문자열이 되어 허브 카드의 출처 표기가 사라진다
        # (2026-08-11 실제 발생 — 캡션에 `· []` 로 찍혔다).
        communities.append(
            {
                "id": r.get("community_id") or "",
                "name": r.get("community_name") or "",
                "posts": posts,
            }
        )

    return {
        "digest_id": window_end.astimezone(KST).strftime("%Y-%m-%d"),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "communities": communities,
    }


def push_digest(
    results: list,
    window_start: datetime,
    window_end: datetime,
    env_path: str = DEFAULT_ENV_PATH,
) -> dict:
    """허브로 push. 예외를 던지지 않고 `{"pushed": bool, "message": str}`를 돌려준다."""
    # get_env_value는 실제 환경변수를 우선하고 .env로 폴백한다. GitHub Actions 러너에는
    # .env 파일이 없고 시크릿이 환경변수로만 주입되므로, load_env만 쓰면 러너에서
    # "미설정 — 생략"으로 조용히 스킵된다(정상 로그처럼 보여 더 위험하다).
    file_data = load_env(env_path)
    base_url = (get_env_value("HUB_BASE_URL", env_path, file_data) or "").rstrip("/")
    secret = get_env_value("HUB_INGEST_SECRET", env_path, file_data) or ""
    if not base_url or not secret:
        return {
            "pushed": False,
            "message": "HUB_BASE_URL/HUB_INGEST_SECRET 미설정 — 허브 push 생략",
        }

    payload = build_payload(results, window_start, window_end)
    item_count = sum(len(c["posts"]) for c in payload["communities"])
    if item_count == 0:
        # 0건도 계약상 유효하지만, 보낼 이유가 없다(허브가 어차피 카드를 만들지 않는다).
        return {"pushed": False, "message": "보낼 글이 0건 — 허브 push 생략"}

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ts = str(int(time.time()))
    signature = hmac.new(
        secret.encode("utf-8"), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()

    request = urllib.request.Request(
        base_url + ENDPOINT_PATH,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Hub-Timestamp": ts,
            "X-Hub-Signature": signature,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        # 응답 본문에 시크릿이 실릴 일은 없지만(허브가 스크러버를 거친다) 길이는 자른다.
        detail = e.read().decode("utf-8", errors="replace")[:200] if e.fp else ""
        logger.warning("허브 push 실패: HTTP %s %s", e.code, detail)
        return {"pushed": False, "message": f"허브 응답 HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        logger.warning("허브 push 실패: %s", e)
        return {"pushed": False, "message": f"허브 연결 실패: {type(e).__name__}"}

    if not parsed.get("updated", False):
        # 이미 카드가 생성된 회차 — 재실행이거나 재시도다. 실패가 아니다.
        reason = parsed.get("reason", "unknown")
        return {
            "pushed": True,
            "message": f"허브가 갱신하지 않음({reason}) — 이미 처리된 회차",
        }
    return {"pushed": True, "message": f"허브 수신 완료 — {item_count}건"}
