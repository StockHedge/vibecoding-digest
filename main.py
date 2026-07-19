#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py — 커뮤니티 인기 글 수집·보고 파이프라인 오케스트레이션.

3일(72시간)마다 실행되어 communities.json에 정의된 커뮤니티들의 최근 72시간
인기 글을 수집하고, 한국어 보고문을 조립해 카카오톡 "나에게 보내기"로 전송한다.

사용:
  py -3 main.py              # 실제 전송(access token 갱신 필요 — kakao_auth.py로 사전 인증)
  py -3 main.py --dry-run    # 전송 대신 보고문을 stdout에 출력(테스트용)

종료 코드:
  0 — 성공. 일부 커뮤니티 수집 실패는 보고문 [수집 실패] 섹션에 명시되며 0으로 취급.
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

import collect
import report
from kakao_sender import DEFAULT_ENV_PATH, is_rotation_pending, send_to_me

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
        maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
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


def run(dry_run: bool, env_path: str = DEFAULT_ENV_PATH,
        communities_path: str = COMMUNITIES_PATH) -> int:
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
        return _deliver(text, dry_run, env_path)

    results = collect.collect_all(communities, env_path=env_path)
    text = report.build_report(results, communities, window_start, window_end)

    ok = sum(1 for r in results if not r.get("error"))
    fail = sum(1 for r in results if r.get("error"))
    logger.info("수집 완료: 성공 %d / 실패 %d (전체 %d)", ok, fail, len(results))

    return _deliver(text, dry_run, env_path)


def _deliver(text: str, dry_run: bool, env_path: str) -> int:
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
    parser = argparse.ArgumentParser(description="커뮤니티 인기 글 수집·보고 파이프라인")
    parser.add_argument("--dry-run", action="store_true",
                        help="전송 대신 보고문을 stdout에 출력")
    parser.add_argument("--env", default=DEFAULT_ENV_PATH, help=".env 경로 (기본 .env)")
    parser.add_argument("--communities", default=COMMUNITIES_PATH,
                        help="커뮤니티 설정 JSON 경로 (기본 communities.json)")
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
        return run(dry_run=args.dry_run, env_path=args.env,
                   communities_path=args.communities)
    except Exception as e:  # noqa: BLE001 - CLI 최상위 경계
        logger.exception("예기치 못한 오류: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(_main())
