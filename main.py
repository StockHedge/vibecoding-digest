#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py — 커뮤니티 인기 글 수집·요약·PDF·게시 파이프라인 오케스트레이션.

3일(72시간)마다 실행되어 communities.json에 정의된 커뮤니티들의 최근 72시간
인기 글을 수집하고, 각 글의 본문을 확보해 Gemini로 한글 번역·요약한 뒤 PDF
문서로 묶어 git 저장소(reports/)에 커밋·푸시하고, 카카오톡 "나에게 보내기"로는
헤더·하이라이트·PDF 링크만 담은 짧은 메시지를 전송한다.

파이프라인: collect → (top_n 슬라이스) → fetch_content → summarize → make_pdf
           → publish(git commit·push) → kakao 전송

사용:
  py -3 main.py                # 실제 전송(access token 갱신 필요 — kakao_auth.py로 사전 인증)
  py -3 main.py --dry-run      # 수집·요약·PDF 생성까지 실행하되 push·카카오 전송은 생략
  py -3 main.py --legacy-text  # 개편 이전의 "조각 텍스트 나열 전송" 경로(호환용, PDF·게시 없음)

종료 코드:
  0 — 성공. 일부 커뮤니티 수집 실패/글 요약 실패는 PDF·보고문에 명시되며 0으로 취급.
  1 — 카카오 전송 자체가 실패한 경우(--dry-run에서는 전송을 하지 않으므로 발생하지 않음).
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import collect
import fetch_content
import gdrive
import hub_sender
import make_pdf
import publish
import report
import summarize
from kakao_sender import (
    DEFAULT_ENV_PATH,
    TEXT_TEMPLATE_MAX,
    is_rotation_pending,
    send_to_me,
)

logger = logging.getLogger("main")

COMMUNITIES_PATH = "communities.json"
LOG_DIR = "logs"
LOG_FILE = "digest.log"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5MB
LOG_BACKUP_COUNT = 3

# 러너에서 refresh token 회전이 GitHub Secret에 반영되지 못했을 때 보내는 경고.
# (다음 실행이 만료 토큰으로 실패하는 것을 막기 위한 수동 갱신 안내 — 토큰 값은 담지 않는다.)
ROTATION_WARNING = (
    "[다이제스트 봇] 카카오 refresh token이 회전되었습니다. "
    "GitHub Secret KAKAO_REFRESH_TOKEN을 수동 갱신해야 다음 실행이 정상 동작합니다."
)


def _setup_logging() -> None:
    """logs/digest.log(RotatingFileHandler 5MB x3) + 콘솔 핸들러 구성."""
    Path(LOG_DIR).mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    file_handler = logging.handlers.RotatingFileHandler(
        str(Path(LOG_DIR) / LOG_FILE),
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    root.addHandler(console_handler)


def load_communities(path: str = COMMUNITIES_PATH) -> list:
    """communities.json을 로드한다(표준 라이브러리 json — PyYAML 등 외부 의존성 금지)."""
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"{path} 파일이 없습니다.")
    with p.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise RuntimeError(f"{path}의 최상위 구조는 배열([...])이어야 합니다.")
    return data


def _slice_top_n(results: list, communities: list) -> None:
    """report.py와 동일한 top_n 규칙으로 각 커뮤니티 결과의 posts를 잘라낸다(in-place).

    이후 단계(fetch_content의 본문 확보, summarize의 Gemini 호출)가 실제로 PDF에
    실릴 글에만 비용을 쓰도록, 수집 직후·본문 확보/요약 이전에 자른다.
    """
    top_n_map = report._top_n_by_id(communities)
    for r in results:
        if r.get("error"):
            continue
        top_n = top_n_map.get(r["community_id"], report.DEFAULT_TOP_N)
        r["posts"] = r["posts"][:top_n]


def _fit_highlight(highlight: dict, budget: int) -> str:
    """'★ 제목 — 요약'을 budget(글자) 안에 맞춰 축약. 예산이 너무 작으면 빈 문자열.

    상세 요약은 PDF에 전부 실리므로, 카카오 본문의 하이라이트는 제목을 우선 보존하고
    남는 만큼만 요약(reason)을 덧붙여 200자 단일 전송을 보장한다.
    """
    star = "★ "
    if budget <= len(star) + 4:
        return ""
    title = " ".join((highlight.get("translated_title") or "").split())
    reason = " ".join((highlight.get("reason") or "").split())
    base = star + title
    if len(base) >= budget:
        return base[: budget - 1] + "…"
    sep = " — "
    room = budget - len(base) - len(sep)
    if reason and room > 8:
        r = reason if len(reason) <= room else reason[: room - 1] + "…"
        return base + sep + r
    return base


def _build_kakao_text(
    window_start: datetime,
    window_end: datetime,
    results: list,
    highlight,
    pdf_path: Path,
    link: Optional[str],
    publish_message: str,
) -> str:
    """카카오 전송용 짧은 메시지(헤더+하이라이트+PDF 링크)를 200자 이내 한 통으로 조립한다.

    헤더·링크를 먼저 확보하고 남는 예산에 하이라이트를 축약해 넣으므로, kakao_sender의
    분할(200자 초과 시 (i/N) 조각화)이 발동하지 않고 단일 메시지로 전송된다.
    """
    ok_results = [r for r in results if not r.get("error")]
    total_posts = sum(len(r["posts"]) for r in ok_results)
    total_summarized = sum(
        1 for r in ok_results for p in r["posts"] if not p.get("summary_failed")
    )

    ws = window_start.astimezone(collect.KST).strftime("%m/%d")
    we = window_end.astimezone(collect.KST).strftime("%m/%d")

    header = [
        f"커뮤니티 다이제스트 ({ws}~{we})",
        f"총 {total_posts}건 · 요약 {total_summarized}건",
    ]
    if link:
        link_line = f"PDF: {link}"
    else:
        link_line = f"PDF(로컬 생성, {publish_message}): {pdf_path}"

    # 고정부(헤더+링크)를 먼저 두고, 남는 예산으로 하이라이트를 축약한다.
    fixed_len = len("\n".join(header + [link_line]))
    hl = (
        _fit_highlight(highlight, TEXT_TEMPLATE_MAX - fixed_len - 1)
        if highlight
        else ""
    )
    lines = header + ([hl] if hl else []) + [link_line]
    return "\n".join(lines)


def run(
    dry_run: bool,
    legacy_text: bool,
    env_path: str = DEFAULT_ENV_PATH,
    communities_path: str = COMMUNITIES_PATH,
) -> int:
    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(hours=collect.WINDOW_HOURS)

    try:
        communities = load_communities(communities_path)
    except Exception as e:
        # 설정 자체가 없으면 수집이 불가능하지만, 실패 사실은 그래도 알린다.
        logger.error("설정 로드 실패: %s", e)
        text = (
            "커뮤니티 인기 글 다이제스트\n\n"
            f"설정 로드 실패로 수집을 진행하지 못했습니다: {e}\n"
        )
        return _deliver_text(text, dry_run, env_path)

    results = collect.collect_all(communities, env_path=env_path)

    ok = sum(1 for r in results if not r.get("error"))
    fail = sum(1 for r in results if r.get("error"))
    logger.info("수집 완료: 성공 %d / 실패 %d (전체 %d)", ok, fail, len(results))

    if legacy_text:
        # 개편 이전 경로: 요약·PDF·게시 없이 조각 텍스트 보고문을 그대로 전송(호환용).
        text = report.build_report(results, communities, window_start, window_end)
        return _deliver_text(text, dry_run, env_path)

    _slice_top_n(results, communities)
    fetch_content.fetch_content_for_results(results, communities)
    highlight = summarize.summarize_all(results, env_path=env_path)

    pdf_path = make_pdf.build_pdf(results, communities, window_start, window_end)
    logger.info("PDF 생성 완료: %s", pdf_path)

    if dry_run:
        print(f"[dry-run] PDF 생성 완료: {pdf_path}")
        if highlight:
            print(
                f"[dry-run] 하이라이트: {highlight['translated_title']} — {highlight['reason']}"
            )
        else:
            print("[dry-run] 하이라이트 없음(요약 성공한 글 없음)")
        logger.info("dry-run 모드: git 게시·카카오 전송 생략")
        return 0

    # for-marketing 허브 push — IG 카드뉴스 원재료(허브 계약 t9-integration.md §6).
    # fail-soft라 허브가 죽어 있어도 아래 Drive·git·카카오 경로는 그대로 진행된다.
    # 이 루틴의 본래 목적은 카카오톡 다이제스트이고, 허브 연동은 부가 경로다.
    hub_result = hub_sender.push_digest(
        results, window_start, window_end, env_path=env_path
    )
    logger.info("허브 push: %s", hub_result["message"])

    # Drive 공개 업로드(카카오 링크용). 실패해도 예외 없이 link=None을 반환하므로
    # 아래에서 GitHub 링크로 자연 폴백한다.
    drive_result = gdrive.publish_to_drive(pdf_path, env_path=env_path)
    if drive_result.get("link"):
        logger.info("Drive 공개 업로드 완료 — 카카오 링크로 사용")
    else:
        logger.warning(
            "Drive 링크 미사용(%s) — GitHub 링크로 폴백", drive_result.get("message")
        )

    # git 아카이브: 링크 목적이 아니라 reports/ 이력 보존 목적으로 유지한다.
    publish_result = publish.publish_pdf(pdf_path)
    logger.info(
        "게시 결과: %s (committed=%s pushed=%s)",
        publish_result["message"],
        publish_result["committed"],
        publish_result["pushed"],
    )

    # 링크 우선순위: Drive 공개 링크 > GitHub blob 링크 > (둘 다 실패 시) 로컬 경로 문구.
    link = drive_result.get("link") or publish_result.get("link")
    kakao_text = _build_kakao_text(
        window_start,
        window_end,
        results,
        highlight,
        pdf_path,
        link,
        publish_result.get("message", ""),
    )
    return _deliver_text(kakao_text, dry_run=False, env_path=env_path)


def _deliver_text(text: str, dry_run: bool, env_path: str) -> int:
    """dry-run이면 stdout 출력, 아니면 카카오 전송. 전 커뮤니티 실패여도 항상 전송 시도."""
    if dry_run:
        print(text)
        logger.info("dry-run 모드: 카카오 전송 생략")
        return 0
    try:
        n = send_to_me(text, env_path=env_path)
        logger.info("카카오 전송 완료: %d개 조각", n)
    except Exception as e:  # noqa: BLE001 - 전송 실패는 종료 코드 1로 상위에 알림
        logger.error("카카오 전송 실패: %s", e)
        return 1

    # 전송 성공 후에만: 러너에서 refresh token 회전이 영속화되지 못했으면 경고 1건 발송.
    # (경고 발송 실패는 본 전송 성공에 영향을 주지 않으므로 종료 코드를 바꾸지 않는다.)
    if is_rotation_pending():
        try:
            send_to_me(ROTATION_WARNING, env_path=env_path)
            logger.warning("refresh token 회전 경고 메시지 발송 완료")
        except Exception as e:  # noqa: BLE001 - 경고 실패는 본 전송 성공에 영향 없음
            logger.error("회전 경고 메시지 발송 실패: %s", e)
    return 0


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="커뮤니티 인기 글 수집·요약·PDF·게시 파이프라인"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="수집·요약·PDF 생성까지 실행하되 push·카카오 전송은 생략",
    )
    parser.add_argument(
        "--legacy-text",
        action="store_true",
        help="개편 이전의 조각 텍스트 나열 전송 경로(호환용, PDF·게시 없음)",
    )
    parser.add_argument("--env", default=DEFAULT_ENV_PATH, help=".env 경로 (기본 .env)")
    parser.add_argument(
        "--communities",
        default=COMMUNITIES_PATH,
        help="커뮤니티 설정 JSON 경로 (기본 communities.json)",
    )
    args = parser.parse_args(argv)

    # Windows 콘솔(cmd.exe 기본 코드페이지 CP949) 대응: 가능하면 stdout/stderr을
    # UTF-8로 재구성해 한글 다이제스트 출력이 깨지지 않도록 한다.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass

    _setup_logging()
    try:
        return run(
            dry_run=args.dry_run,
            legacy_text=args.legacy_text,
            env_path=args.env,
            communities_path=args.communities,
        )
    except Exception as e:  # noqa: BLE001 - CLI 최상위 경계
        logger.exception("예기치 못한 오류: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(_main())
