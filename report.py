#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report.py — 수집 결과(collect.collect_all 반환값)를 한국어 보고문으로 조립한다.

구성: 헤더(수집 창 KST) → 커뮤니티별 섹션(이름 + 상위 top_n건) → 실패 섹션 → 푸터(총 N건).
카카오 전송 시 200자 초과분의 분할은 kakao_sender가 처리하므로, 여기서는 길이
제한을 신경 쓰지 않는다.
"""
from __future__ import annotations

from datetime import datetime, timezone

from collect import KST

DEFAULT_TOP_N = 3
ERROR_DISPLAY_LIMIT = 120  # 실패 사유는 "1줄"로 짧게(HTTP 오류 본문 등 장문 방지)


def _one_line_error(msg: str, limit: int = ERROR_DISPLAY_LIMIT) -> str:
    """실패 사유를 한 줄로 축약. HTTP 오류 응답 본문 등 장문 메시지가 그대로
    카카오 메시지에 노출되지 않도록 개행을 공백으로 접고 길이를 제한한다."""
    flat = " ".join(msg.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _fmt_kst(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def _top_n_by_id(communities: list) -> dict:
    out = {}
    for c in communities:
        cid = c.get("id")
        if cid is not None:
            out[cid] = int(c.get("top_n") or DEFAULT_TOP_N)
    return out


def build_report(results: list, communities: list,
                  window_start: datetime, window_end: datetime) -> str:
    """수집 결과로 한국어 보고문 전체를 조립해 반환한다."""
    top_n_map = _top_n_by_id(communities)

    ok_results = [r for r in results if not r.get("error")]
    fail_results = [r for r in results if r.get("error")]

    lines = [
        "커뮤니티 인기 글 다이제스트",
        f"수집 창: {_fmt_kst(window_start)} ~ {_fmt_kst(window_end)} (KST)",
        "",
    ]

    total_posts = 0
    for r in ok_results:
        top_n = top_n_map.get(r["community_id"], DEFAULT_TOP_N)
        posts = r["posts"][:top_n]
        lines.append(f"■ {r['community_name']}")
        if not posts:
            lines.append("  (72시간 내 수집된 글 없음)")
        for i, p in enumerate(posts, start=1):
            lines.append(f"{i}. {p['title']} — {p['score_label']}")
            lines.append(f"   {p['url']}")
            total_posts += 1
        lines.append("")

    if fail_results:
        lines.append("[수집 실패]")
        for r in fail_results:
            lines.append(f"- {r['community_name']}: {_one_line_error(r['error'])}")
        lines.append("")

    lines.append(f"총 {total_posts}건 (성공 {len(ok_results)}개 커뮤니티 / 실패 {len(fail_results)}개)")
    return "\n".join(lines).rstrip("\n") + "\n"
