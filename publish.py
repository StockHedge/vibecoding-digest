#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
publish.py — 생성된 다이제스트 PDF를 git commit·push하고 카카오용 GitHub blob 링크를 만든다.

동작:
  1. git add <reports/파일> → git commit.
  2. origin이 설정돼 있으면 git push, 없으면 커밋만 하고 반환값에 "푸시 스킵" 표시.
  3. GITHUB_ACTIONS=true 환경(리포지토리에 커밋 identity가 없는 러너)에서는 커밋 전
     git config user.name/user.email을 github-actions[bot]로 로컬(repo 한정) 설정한다.
  4. 카카오용 링크: origin URL(https/ssh 모두 처리)에서 owner/repo를 파싱해
     https://github.com/<owner>/<repo>/blob/main/reports/<파일명> 형태로 만든다
     (브랜치는 main으로 고정 — 이 파이프라인은 main에 직접 push하는 것을 전제).

git 자체가 실패해도(커밋할 변경 없음/네트워크 오류 등) 예외를 던지지 않고 결과
dict에 사유를 담아 반환한다 — main.py가 이를 보고 카카오 메시지의 링크 문구를
결정한다(링크 없으면 로컬 경로 문구로 대체).
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("publish")

GIT_TIMEOUT = 30    # 초 (add/commit/remote 등 로컬 조회성 명령)
PUSH_TIMEOUT = 60   # 초 (네트워크를 타는 push)
GITHUB_BLOB_BRANCH = "main"

_REMOTE_URL_RE = re.compile(
    r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:)"
    r"(?P<owner>[^/]+)/(?P<repo>.+?)(?:\.git)?/?$"
)


def _short(text: Optional[str], limit: int = 200) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _run_git(args: list, cwd: str, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """git 서브커맨드 실행. 실행 자체가 안 되면(git 미설치 등) rc=1의 가짜 결과로 흡수."""
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, text=True, capture_output=True, timeout=timeout,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr=str(e))


def _relative_path(pdf_path, repo_dir: str) -> str:
    p = Path(pdf_path).resolve()
    base = Path(repo_dir).resolve()
    try:
        return str(p.relative_to(base))
    except ValueError:
        return str(p)


def _parse_owner_repo(remote_url: str) -> Optional[tuple]:
    """github.com 원격 URL(https/ssh)에서 (owner, repo)를 추출. 매칭 실패 시 None."""
    m = _REMOTE_URL_RE.match(remote_url.strip())
    if not m:
        return None
    return m.group("owner"), m.group("repo")


def publish_pdf(pdf_path, repo_dir: str = ".") -> dict:
    """PDF를 git commit·push하고 결과를 dict로 반환한다.

    반환: {"committed": bool, "pushed": bool, "link": str | None, "message": str}
    link은 push까지 성공하고 origin이 github.com이어야만 채워진다.
    """
    rel_path = _relative_path(pdf_path, repo_dir)

    if os.environ.get("GITHUB_ACTIONS") == "true":
        # 러너 컨테이너에는 커밋용 git identity가 없어 매 실행 전 로컬(repo 한정)로 설정한다.
        _run_git(["config", "user.name", "github-actions[bot]"], repo_dir)
        _run_git(["config", "user.email", "github-actions[bot]@users.noreply.github.com"], repo_dir)

    add_proc = _run_git(["add", rel_path], repo_dir)
    if add_proc.returncode != 0:
        msg = f"git add 실패: {_short(add_proc.stderr)}"
        logger.error(msg)
        return {"committed": False, "pushed": False, "link": None, "message": msg}

    commit_msg = f"다이제스트 PDF 추가: {Path(rel_path).name}"
    commit_proc = _run_git(["commit", "-m", commit_msg], repo_dir)
    committed = commit_proc.returncode == 0
    if not committed:
        combined = _short((commit_proc.stdout or "") + (commit_proc.stderr or ""))
        if "nothing to commit" in combined.lower():
            logger.info("커밋할 변경 사항 없음(이미 최신 상태) — 기존 커밋 기준으로 진행")
        else:
            msg = f"git commit 실패: {combined}"
            logger.error(msg)
            return {"committed": False, "pushed": False, "link": None, "message": msg}
    else:
        logger.info("git commit 완료: %s", rel_path)

    remote_proc = _run_git(["remote", "get-url", "origin"], repo_dir)
    if remote_proc.returncode != 0 or not remote_proc.stdout.strip():
        msg = "푸시 스킵: origin 미설정"
        logger.warning(msg)
        return {"committed": committed, "pushed": False, "link": None, "message": msg}
    remote_url = remote_proc.stdout.strip()

    push_proc = _run_git(["push", "origin", "HEAD"], repo_dir, timeout=PUSH_TIMEOUT)
    if push_proc.returncode != 0:
        msg = f"git push 실패: {_short(push_proc.stderr)}"
        logger.error(msg)
        return {"committed": committed, "pushed": False, "link": None, "message": msg}
    logger.info("git push 완료")

    owner_repo = _parse_owner_repo(remote_url)
    link = None
    if owner_repo:
        owner, repo = owner_repo
        link = (
            f"https://github.com/{owner}/{repo}/blob/{GITHUB_BLOB_BRANCH}/"
            f"{rel_path.replace(os.sep, '/')}"
        )
    else:
        logger.warning("origin URL에서 owner/repo 파싱 실패(github.com 형식이 아님) — 링크 생략")

    return {"committed": committed, "pushed": True, "link": link, "message": "게시 완료"}


# ── CLI (수동 게시/검증용) ───────────────────────────────────────────
def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="다이제스트 PDF git 게시 모듈")
    parser.add_argument("pdf_path", help="게시할 PDF 경로(예: reports/digest-2026-07-19.pdf)")
    parser.add_argument("--repo-dir", default=".", help="git 저장소 루트 (기본 현재 디렉터리)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    result = publish_pdf(args.pdf_path, repo_dir=args.repo_dir)
    print(result["message"])
    print(f"  committed={result['committed']} pushed={result['pushed']}")
    print(f"  link={result['link']}")
    return 0 if result["committed"] or "nothing to commit" in result["message"].lower() else 1


if __name__ == "__main__":
    sys.exit(_main())
