#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gdrive.py — 생성된 다이제스트 PDF를 사용자 Google Drive에 업로드하고
'링크가 있는 모든 사용자' 공개 권한을 부여해 카카오용 공개 view 링크를 반환한다.

왜 서비스계정이 아니라 OAuth인가:
  서비스계정은 Drive 저장 용량이 0이라 개인 Google Drive(My Drive)에 파일을 올릴 수 없고,
  공유 드라이브(Shared Drive)는 Google Workspace 전용이다. 개인 Gmail에서 동작하는 유일한
  방법은 사용자 본인 OAuth 자격증명으로 본인 Drive에 올리는 것이다(kakao와 동일한
  refresh token 패턴).

공식 REST(2026-07 확인):
  - 토큰: POST https://oauth2.googleapis.com/token
          grant_type=refresh_token, client_id, client_secret, refresh_token
  - 업로드: POST https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart
           multipart/related(metadata JSON + 미디어), Authorization: Bearer
  - 공개: POST .../drive/v3/files/{id}/permissions  {"role":"reader","type":"anyone"}
  - 링크: 업로드 응답 fields=id,webViewLink 로 즉시 획득

권한(scope): https://www.googleapis.com/auth/drive.file (앱이 만든 파일만 — Drive 전체 접근 아님)

보안:
  - 토큰/시크릿 값을 stdout/log에 노출하지 않는다(마스킹 상태만).
  - 실패해도 예외를 전파하지 않고 결과 dict에 사유를 담아 반환한다(main이 GitHub 링크로 폴백).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 공통 유틸/상수는 재사용 모듈에서 가져온다(중복 방지 — kakao_auth와 동일 관행).
from kakao_sender import (
    BACKOFF_BASE,
    DEFAULT_ENV_PATH,
    HTTP_RETRIES,
    HTTP_TIMEOUT,
    get_env_value,
    load_env,
    mask_status,
)

logger = logging.getLogger("gdrive")

# ── 공식 문서 검증 상수 ──────────────────────────────────────────────
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_UPLOAD_URL = (
    "https://www.googleapis.com/upload/drive/v3/files"
    "?uploadType=multipart&fields=id,webViewLink"
)
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"

UPLOAD_TIMEOUT = 60.0  # 초 (미디어 업로드는 토큰/권한 호출보다 넉넉히)

ENV_CLIENT_ID = "GDRIVE_CLIENT_ID"
ENV_CLIENT_SECRET = "GDRIVE_CLIENT_SECRET"
ENV_REFRESH_TOKEN = "GDRIVE_REFRESH_TOKEN"
ENV_FOLDER_ID = "GDRIVE_FOLDER_ID"  # 선택 — 지정 시 해당 폴더에 업로드, 없으면 My Drive 루트

_BOUNDARY = "vibecoding-digest-boundary-7f3a9c1d"


# ── HTTP (urllib, Bearer/JSON/멀티파트 겸용) ─────────────────────────
def _request(url: str, *, method: str = "GET", headers=None, body=None,
             timeout: float = HTTP_TIMEOUT, retries: int = HTTP_RETRIES,
             backoff_base: float = BACKOFF_BASE):
    """body는 bytes 또는 None. 일시 오류(429/5xx/네트워크)만 지수 백오프 재시도.

    4xx(400/401/403 등)는 파라미터/인증 문제라 즉시 실패. 응답 본문은 예외에 300자로 절단.
    반환 (status:int, text:str).
    """
    attempt = 0
    while True:
        try:
            req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            transient = e.code == 429 or 500 <= e.code < 600
            if not transient or attempt >= retries:
                snippet = err_body[:300] + ("…" if len(err_body) > 300 else "")
                raise RuntimeError(f"HTTP {e.code}: {snippet}") from e
            logger.warning("HTTP %s 일시 오류, 재시도 %d/%d", e.code, attempt + 1, retries)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt >= retries:
                raise RuntimeError(f"네트워크 오류: {e}") from e
            logger.warning("네트워크 오류(%s), 재시도 %d/%d", e, attempt + 1, retries)
        attempt += 1
        time.sleep(backoff_base * (2 ** (attempt - 1)))


# ── 토큰 갱신 ────────────────────────────────────────────────────────
def refresh_access_token(env_path: str = DEFAULT_ENV_PATH) -> str:
    """GDRIVE refresh_token으로 access token을 갱신해 반환.

    Google 데스크톱 앱 refresh token은 통상 회전하지 않으므로 별도 영속화는 없다.
    invalid_grant(만료/폐기)는 재인증 안내로 변환한다.
    """
    fd = load_env(env_path)
    client_id = get_env_value(ENV_CLIENT_ID, env_path, fd)
    client_secret = get_env_value(ENV_CLIENT_SECRET, env_path, fd)
    refresh_token = get_env_value(ENV_REFRESH_TOKEN, env_path, fd)

    missing = [k for k, v in (
        (ENV_CLIENT_ID, client_id),
        (ENV_CLIENT_SECRET, client_secret),
        (ENV_REFRESH_TOKEN, refresh_token),
    ) if not v]
    if missing:
        raise RuntimeError(
            f"{', '.join(missing)} 미설정 — .env를 확인하거나 `py -3 gdrive_auth.py`로 인증하세요."
        )

    payload = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }
    logger.info("Drive access token 갱신 요청 (refresh_token %s)", mask_status(refresh_token))
    try:
        _status, text = _request(
            GOOGLE_TOKEN_URL, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urllib.parse.urlencode(payload).encode("utf-8"),
        )
    except RuntimeError as e:
        if "invalid_grant" in str(e):
            raise RuntimeError(
                "GDRIVE_REFRESH_TOKEN 만료/폐기(invalid_grant) — `py -3 gdrive_auth.py`로 "
                "재인증하세요. (OAuth 동의화면이 '테스트' 단계면 7일 만에 만료됩니다 — "
                "'프로덕션'으로 게시하세요.)"
            ) from e
        raise

    access_token = json.loads(text).get("access_token")
    if not access_token:
        raise RuntimeError("토큰 응답에 access_token 없음")
    return access_token


# ── 업로드 / 공개 ────────────────────────────────────────────────────
def _upload(pdf_path, access_token: str, folder_id=None) -> tuple:
    """multipart/related로 PDF를 업로드한다. 반환 (file_id, web_view_link)."""
    pdf_bytes = Path(pdf_path).read_bytes()
    metadata: dict = {"name": Path(pdf_path).name}
    if folder_id:
        metadata["parents"] = [folder_id]

    pre = (
        f"--{_BOUNDARY}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata, ensure_ascii=False)}\r\n"
        f"--{_BOUNDARY}\r\n"
        f"Content-Type: application/pdf\r\n\r\n"
    ).encode("utf-8")
    post = f"\r\n--{_BOUNDARY}--\r\n".encode("utf-8")
    body = pre + pdf_bytes + post

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": f"multipart/related; boundary={_BOUNDARY}",
    }
    _status, text = _request(DRIVE_UPLOAD_URL, method="POST", headers=headers,
                             body=body, timeout=UPLOAD_TIMEOUT)
    parsed = json.loads(text)
    file_id = parsed.get("id")
    if not file_id:
        raise RuntimeError(f"업로드 응답에 파일 id 없음: {text[:200]}")
    return file_id, parsed.get("webViewLink")


def _make_public(file_id: str, access_token: str) -> None:
    """'링크가 있는 모든 사용자' 읽기 권한을 부여한다."""
    url = f"{DRIVE_FILES_URL}/{file_id}/permissions"
    body = json.dumps({"role": "reader", "type": "anyone"}).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    _request(url, method="POST", headers=headers, body=body)


def publish_to_drive(pdf_path, env_path: str = DEFAULT_ENV_PATH) -> dict:
    """PDF를 Drive에 올리고 공개 링크를 만든다. 실패해도 예외 없이 dict를 반환한다.

    반환: {"link": str | None, "file_id": str | None, "message": str}
    link은 업로드·공개까지 성공해야만 채워진다. 미설정/실패 시 link=None으로
    호출측(main.py)이 GitHub 링크로 폴백하게 한다.
    """
    try:
        access_token = refresh_access_token(env_path)
        file_id, link = _upload(pdf_path, access_token,
                                 folder_id=get_env_value(ENV_FOLDER_ID, env_path))
        _make_public(file_id, access_token)
        logger.info("Drive 업로드·공개 완료")
        return {"link": link, "file_id": file_id, "message": "Drive 게시 완료"}
    except Exception as e:  # noqa: BLE001 - 게시 실패는 폴백을 위해 흡수(전송을 막지 않음)
        logger.warning("Drive 게시 실패: %s", e)
        return {"link": None, "file_id": None, "message": f"Drive 게시 실패: {e}"}


# ── CLI (수동 검증/테스트용) ─────────────────────────────────────────
def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Google Drive 업로드/공개 링크 모듈")
    parser.add_argument("--check", action="store_true",
                        help="GDRIVE 키 설정 상태만 마스킹 진단(업로드 안 함)")
    parser.add_argument("--test", metavar="PDF", help="지정 PDF를 실제 업로드·공개(테스트)")
    parser.add_argument("--env", default=DEFAULT_ENV_PATH, help=".env 경로 (기본 .env)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.check:
        fd = load_env(args.env)
        for k in (ENV_CLIENT_ID, ENV_CLIENT_SECRET, ENV_REFRESH_TOKEN, ENV_FOLDER_ID):
            print(f"{k}: {mask_status(get_env_value(k, args.env, fd))}")
        return 0

    if args.test:
        result = publish_to_drive(args.test, env_path=args.env)
        print(result["message"])
        print(f"  link={result['link']}")
        return 0 if result["link"] else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
