#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gdrive_auth.py — Google Drive OAuth 1회용 부트스트랩 CLI (kakao_auth.py와 동일 구조).

동작:
  1. .env의 GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET를 읽는다(없으면 명확한 에러).
  2. 로컬 콜백 서버(http://localhost:8891/callback)를 띄운다.
  3. 브라우저로 Google 동의(scope=drive.file, access_type=offline, prompt=consent)를 열어
     사용자 동의 → 인가 코드 수신.
  4. 인가 코드를 access/refresh 토큰으로 교환한다.
  5. .env에 GDRIVE_REFRESH_TOKEN을 저장한다(기존 키 보존, 해당 키만 갱신).

⚠️ OAuth 동의화면을 '프로덕션'으로 게시하지 않으면 refresh token이 7일 만에 만료된다
   (테스트 단계 = 7일 만료 사고 유발 — 반드시 프로덕션 게시).

공식 문서(2026-07 확인):
  - 인가: https://accounts.google.com/o/oauth2/v2/auth
          ?client_id&redirect_uri&response_type=code&scope&access_type=offline&prompt=consent
  - 토큰: https://oauth2.googleapis.com/token (grant_type=authorization_code)
  - 데스크톱 앱 OAuth 클라이언트는 loopback redirect(http://localhost:PORT)를 허용한다.

보안:
  - state(CSRF) 파라미터를 생성·검증한다.
  - 콜백 서버 접근 로그를 억제한다(URL의 인가 코드 노출 방지).
  - 토큰 값은 stdout/log에 노출하지 않고 마스킹 상태만 표시한다.
  - 콜백 서버는 127.0.0.1에만 바인딩한다.
"""
from __future__ import annotations

import argparse
import http.server
import json
import logging
import secrets
import sys
import threading
import urllib.parse
import webbrowser

from gdrive import (
    ENV_CLIENT_ID,
    ENV_CLIENT_SECRET,
    ENV_REFRESH_TOKEN,
    GOOGLE_TOKEN_URL,
)
from kakao_sender import (
    DEFAULT_ENV_PATH,
    _http_request,
    get_env_value,
    load_env,
    mask_status,
    update_env_var,
)

logger = logging.getLogger("gdrive_auth")

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
DEFAULT_REDIRECT_URI = "http://localhost:8891/callback"  # kakao(8888)와 포트 분리
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8891
CALLBACK_PATH = "/callback"
SCOPE = "https://www.googleapis.com/auth/drive.file"  # 앱이 만든 파일만
WAIT_TIMEOUT = 300  # 인가 코드 수신 대기 최대 시간(초)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """OAuth 리다이렉트를 1회 수신하는 최소 핸들러."""

    result: dict = {}
    expected_state: str = ""
    done_event: threading.Event = None  # type: ignore[assignment]

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 규약
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.result = {
            "code": (params.get("code") or [None])[0],
            "error": (params.get("error") or [None])[0],
            "state": (params.get("state") or [None])[0],
        }
        ok = bool(_CallbackHandler.result["code"]) and (
            _CallbackHandler.result["state"] == _CallbackHandler.expected_state
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = ("인증이 완료되었습니다. 이 창을 닫고 터미널로 돌아가세요."
               if ok else "인증에 실패했습니다. 터미널의 오류 메시지를 확인하세요.")
        self.wfile.write(
            f"<html><body style='font-family:sans-serif;padding:2rem'>"
            f"<h2>{msg}</h2></body></html>".encode("utf-8")
        )
        if _CallbackHandler.done_event is not None:
            _CallbackHandler.done_event.set()

    def log_message(self, *args):
        # 기본 접근 로그 억제: 요청 라인에 담긴 인가 코드가 콘솔에 노출되지 않도록.
        pass


def bootstrap(env_path: str = DEFAULT_ENV_PATH,
              redirect_uri: str = DEFAULT_REDIRECT_URI,
              no_browser: bool = False) -> None:
    """OAuth 부트스트랩 전체 흐름 실행."""
    file_data = load_env(env_path)
    client_id = get_env_value(ENV_CLIENT_ID, env_path, file_data)
    client_secret = get_env_value(ENV_CLIENT_SECRET, env_path, file_data)

    if not client_id or not client_secret:
        raise RuntimeError(
            f"{ENV_CLIENT_ID}/{ENV_CLIENT_SECRET} 미설정 — GCP에서 OAuth 클라이언트(데스크톱 앱)를 "
            "만들어 .env에 넣으세요. (.env.example 참고, Drive API 사용설정·동의화면 프로덕션 게시 필수)"
        )

    state = secrets.token_urlsafe(16)
    _CallbackHandler.expected_state = state
    _CallbackHandler.done_event = threading.Event()
    _CallbackHandler.result = {}

    try:
        server = http.server.HTTPServer((CALLBACK_HOST, CALLBACK_PORT), _CallbackHandler)
    except OSError as e:
        raise RuntimeError(
            f"콜백 포트 {CALLBACK_PORT} 바인딩 실패({e}). 다른 프로세스가 점유 중인지 확인하세요."
        ) from e

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("콜백 서버 시작: %s", redirect_uri)

    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",  # refresh_token 발급에 필수
        "prompt": "consent",       # 매 인증마다 refresh_token 확실히 재발급
        "state": state,
    }
    auth_url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(auth_params)}"

    print("아래 URL을 브라우저에서 열어 Google 로그인/동의를 완료하세요:")
    print(auth_url)
    if not no_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:  # noqa: BLE001 - 브라우저 자동 실행 실패는 치명적이지 않음
            logger.info("브라우저 자동 실행 실패 — 위 URL을 수동으로 여세요.")

    try:
        got = _CallbackHandler.done_event.wait(timeout=WAIT_TIMEOUT)
    finally:
        server.shutdown()
        server.server_close()

    if not got:
        raise RuntimeError(f"인가 코드 수신 시간 초과({WAIT_TIMEOUT}초). 다시 시도하세요.")

    res = _CallbackHandler.result
    if res.get("error"):
        raise RuntimeError(f"인가 실패: {res['error']}")
    if res.get("state") != state:
        raise RuntimeError("state 불일치 — CSRF 의심으로 중단합니다.")
    code = res.get("code")
    if not code:
        raise RuntimeError("인가 코드를 받지 못했습니다.")

    logger.info("인가 코드 수신 완료, 토큰 교환 중...")
    payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code,
    }
    _status, body = _http_request(
        GOOGLE_TOKEN_URL, data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    parsed = json.loads(body)
    refresh_token = parsed.get("refresh_token")
    access_token = parsed.get("access_token")
    if not refresh_token:
        raise RuntimeError(
            f"토큰 응답에 refresh_token 없음: {parsed.get('error', 'unknown')} "
            "(이미 동의한 앱이면 https://myaccount.google.com/permissions 에서 접근 권한 삭제 후 재시도)"
        )

    update_env_var(ENV_REFRESH_TOKEN, refresh_token, env_path)
    logger.info("%s 저장 완료 (%s)", ENV_REFRESH_TOKEN, mask_status(refresh_token))

    # 값이 아닌 마스킹 상태·만료 정보만 출력.
    print("인증 완료.")
    print(f"  refresh_token: {mask_status(refresh_token)}")
    print(f"  access_token: {mask_status(access_token)} (유효 {parsed.get('expires_in')}초)")
    print(f"  저장 위치: {env_path} ({ENV_REFRESH_TOKEN})")


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Google Drive OAuth 1회용 부트스트랩")
    parser.add_argument("--env", default=DEFAULT_ENV_PATH, help=".env 경로 (기본 .env)")
    parser.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI,
                        help=f"OAuth 클라이언트에 등록된 Redirect URI (기본 {DEFAULT_REDIRECT_URI})")
    parser.add_argument("--no-browser", action="store_true",
                        help="브라우저 자동 실행 없이 URL만 출력")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        bootstrap(env_path=args.env, redirect_uri=args.redirect_uri,
                  no_browser=args.no_browser)
    except Exception as e:  # noqa: BLE001 - CLI 최상위 경계
        logger.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
