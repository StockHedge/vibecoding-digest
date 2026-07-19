#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kakao_sender.py — 카카오톡 "나에게 보내기" 재사용 전송 모듈.

공식 문서(developers.kakao.com, 2026-07 확인) 검증 사실:
- 엔드포인트: POST https://kapi.kakao.com/v2/api/talk/memo/default/send
- 헤더: Authorization: Bearer <access_token>,
        Content-Type: application/x-www-form-urlencoded;charset=utf-8
- 본문: template_object (JSON 문자열). text 템플릿은 text 필드 최대 200자, link 필수.
- 성공 응답: {"result_code": 0} (HTTP 200)
- 필요 권한(scope): talk_message
- 토큰: refresh_token 유효 5184000초(60일), access_token 약 6시간.
        토큰 갱신 시 refresh_token 잔여 유효기간이 1개월 미만이면 새 refresh_token 동봉.

설계 근거:
- list 템플릿은 항목(contents)이 최소 2 ~ 최대 3개로 제한되어 긴 보고문 전송에 부적합.
  따라서 text 템플릿을 쓰고, 200자 초과 시 자동 분할 후 각 조각에 (i/N) 표기해 순차 전송.
- 외부 의존성 없이 표준 라이브러리(urllib)만 사용.

보안:
- .env 전체를 stdout에 노출하지 않는다. 진단은 키별 마스킹(mask_status).
- 토큰 값을 로그에 남기지 않는다(마스킹된 상태 문자열만 기록).

알려진 한계(의도적 미조치 — 단일 사용자·로컬 실행 전제):
- kakao_auth.py는 인가 URL을 stdout에 출력한다. URL에 REST API 키가 client_id로
  포함되므로, 콘솔 출력을 로그 파일로 리다이렉트할 때는 유의한다(로컬 콘솔 한정 위험).
- .env 갱신은 단일 프로세스 순차 사용을 전제한다. 여러 프로세스가 동시에 회전 저장하면
  마지막 쓰기만 남는 레이스가 가능하나, 개인 자동화 용도에서는 사실상 발생하지 않는다.
- 로컬 콜백(localhost)은 동일 호스트의 다른 프로세스가 이론상 인가 코드를 주입할 수 있다.
  state(CSRF) 검증으로 완화하지만 완전 차단은 아니다.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

# 모듈 로거. 핸들러(파일/스트림) 구성은 호출측 책임(라이브러리 관행).
logger = logging.getLogger("kakao_sender")

# ── 공식 문서 검증 상수 ──────────────────────────────────────────────
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
MEMO_SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"

TEXT_TEMPLATE_MAX = 200          # text 템플릿 text 필드 최대 글자 수(문서 확인)
CHUNK_BUDGET = 185               # 분할 시 조각 본문 예산(200 - (i/N) 표기 여유분)

HTTP_TIMEOUT = 10.0              # 초
HTTP_RETRIES = 2                 # 일시적 오류 시 지수 백오프 재시도 횟수
BACKOFF_BASE = 1.0              # 초 (1s → 2s)

# text 템플릿은 link가 필수 필드다. 자기 메모용 기본 링크(버튼 "자세히 보기" 대상).
DEFAULT_LINK = {
    "web_url": "https://developers.kakao.com",
    "mobile_web_url": "https://developers.kakao.com",
}

DEFAULT_ENV_PATH = ".env"
ENV_REST_API_KEY = "KAKAO_REST_API_KEY"
ENV_REFRESH_TOKEN = "KAKAO_REFRESH_TOKEN"
ENV_CLIENT_SECRET = "KAKAO_CLIENT_SECRET"  # 콘솔에서 Client Secret 활성화 시에만 필요(선택)
ENV_ADMIN_TOKEN = "ADMIN_TOKEN"            # GitHub repo secrets write 권한 PAT(러너 전용)

GITHUB_SECRET_UPDATE_TIMEOUT = 30          # gh secret set 호출 타임아웃(초)

# refresh token이 회전됐으나 러너 환경에서 영속화(GitHub Secret 갱신)에 실패했을 때 True.
# 호출측(main.py)이 전송 성공 후 카카오 경고 메시지를 1건 보내도록 하는 신호.
# (모듈 전역 상태 — is_rotation_pending()으로 조회한다.)
rotation_pending = False


# ── .env 유틸 ────────────────────────────────────────────────────────
def load_env(path: str = DEFAULT_ENV_PATH) -> dict:
    """`.env`를 파싱해 key→value dict로 반환. 값은 절대 로그/출력하지 않는다."""
    data: dict = {}
    p = Path(path)
    if not p.exists():
        return data
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            data[key] = val
    return data


def get_env_value(key: str, env_path: str = DEFAULT_ENV_PATH,
                  file_data: Optional[dict] = None) -> Optional[str]:
    """실제 환경변수를 우선하고, 없으면 .env 파일 값을 반환. 빈 값은 None 처리."""
    v = os.environ.get(key)
    if v:
        return v
    if file_data is None:
        file_data = load_env(env_path)
    return file_data.get(key) or None


def update_env_var(key: str, value: str, path: str = DEFAULT_ENV_PATH) -> None:
    """`.env`에서 해당 key 한 줄만 갱신(기존 키·주석·순서 보존). 없으면 append.

    값 자체는 어떤 경우에도 stdout/log로 출력하지 않는다.
    """
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    new_line = f"{key}={value}"
    replaced = False
    out = []
    for raw in lines:
        stripped = raw.strip()
        candidate = (stripped[len("export "):].strip()
                     if stripped.startswith("export ") else stripped)
        if candidate.startswith(key + "=") and not replaced:
            out.append(new_line)
            replaced = True
        else:
            out.append(raw)
    if not replaced:
        out.append(new_line)

    # 원자적 쓰기: 같은 디렉터리에 임시파일로 기록 후 os.replace로 교체한다.
    # (쓰기 도중 크래시해도 .env 원본이 반쪽 상태로 남지 않도록)
    content = "\n".join(out) + "\n"
    directory = str(p.parent) if str(p.parent) else "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if os.name != "nt":
            # POSIX: 소유자만 읽기/쓰기(0o600). Windows는 부모 디렉터리 ACL 상속 유지.
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, str(p))
    except Exception:
        # 교체 실패 시 임시파일이 남지 않도록 정리 후 재전파.
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def mask_status(value: Optional[str]) -> str:
    """토큰/키 진단용 마스킹 문자열. 값 자체는 절대 노출하지 않는다."""
    if not value:
        return "<empty> (0 chars)"
    return f"set ({len(value)} chars)"


# ── HTTP (urllib) ────────────────────────────────────────────────────
def _http_request(url: str, data: Optional[dict] = None,
                  headers: Optional[dict] = None,
                  timeout: float = HTTP_TIMEOUT,
                  retries: int = HTTP_RETRIES,
                  backoff_base: float = BACKOFF_BASE):
    """urllib 기반 HTTP 호출. data가 있으면 POST(form-urlencoded), 없으면 GET.

    - timeout 적용.
    - 일시적 오류(네트워크·타임아웃·HTTP 5xx·429)에만 지수 백오프 재시도.
    - 그 외 4xx(400/401 등)는 파라미터/인증 문제라 재시도로 풀리지 않으므로 즉시 실패.
    반환: (status_code:int, body:str). 최종 실패 시 RuntimeError.
    """
    body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    attempt = 0
    while True:
        try:
            req = urllib.request.Request(url, data=body, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            transient = e.code == 429 or 500 <= e.code < 600
            if not transient or attempt >= retries:
                # 4xx 등 비일시적 오류: 응답 본문을 최대 300자로 절단해 상위로 전달
                # (외부 응답을 예외/로그에 원문 전재하지 않도록 위생 처리).
                snippet = err_body[:300] + ("…" if len(err_body) > 300 else "")
                raise RuntimeError(f"HTTP {e.code}: {snippet}") from e
            logger.warning("HTTP %s 일시 오류, 재시도 %d/%d", e.code, attempt + 1, retries)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt >= retries:
                raise RuntimeError(f"네트워크 오류: {e}") from e
            logger.warning("네트워크 오류(%s), 재시도 %d/%d", e, attempt + 1, retries)
        attempt += 1
        time.sleep(backoff_base * (2 ** (attempt - 1)))


# ── 토큰 회전 영속화 ─────────────────────────────────────────────────
def is_rotation_pending() -> bool:
    """refresh token이 회전됐으나 러너 환경에서 영속화하지 못했는지 여부.

    True이면 호출측(main.py)이 전송 성공 후 수동 갱신 경고 메시지를 보내야 한다.
    """
    return rotation_pending


def _update_github_secret(name: str, value: str, admin_token: str) -> bool:
    """gh CLI로 repo secret을 갱신한다. 성공 시 True.

    - 토큰 값은 argv가 아닌 stdin으로 전달한다(프로세스 목록/ps 노출 방지).
    - PAT은 GH_TOKEN 환경변수로 주입한다(argv 미노출).
    - 대상 repo는 GITHUB_REPOSITORY(러너 기본 컨텍스트)를 --repo로 명시한다.
    - gh 미설치·권한 부족·타임아웃 등 어떤 실패도 False로 흡수해 전송을 막지 않는다.
    """
    args = ["gh", "secret", "set", name]
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        args += ["--repo", repo]
    env = {**os.environ, "GH_TOKEN": admin_token}
    try:
        proc = subprocess.run(
            args, input=value, text=True, capture_output=True,
            env=env, timeout=GITHUB_SECRET_UPDATE_TIMEOUT,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as e:
        logger.warning("gh secret set 실행 실패(%s)", type(e).__name__)
        return False
    if proc.returncode != 0:
        # stderr에 토큰 값은 담기지 않으나, 안전하게 앞부분만 절단해 로깅.
        logger.warning("gh secret set 비정상 종료(rc=%s): %s",
                       proc.returncode, (proc.stderr or "").strip()[:200])
        return False
    return True


def _persist_rotated_refresh_token(new_refresh: str,
                                   env_path: str = DEFAULT_ENV_PATH) -> None:
    """회전된 refresh_token을 실행 환경에 맞게 영속화한다.

    - 먼저 현재 프로세스의 os.environ을 갱신해, 같은 실행 내 이후 호출이 최신 토큰을
      쓰도록 한다(값은 로그/stdout에 노출하지 않음).
    - GitHub Actions 러너(GITHUB_ACTIONS=true): 러너 파일시스템은 휘발성이라 .env 기록은
      다음 실행에 유실된다. ADMIN_TOKEN이 있으면 gh로 repo secret을 갱신하고, 없거나
      실패하면 rotation_pending 플래그를 세워 호출측이 경고를 보내게 한다.
      회전 자체가 전송을 막아서는 안 되므로 실패는 모두 흡수한다.
    - 로컬(GITHUB_ACTIONS 미설정): 기존대로 .env를 원자적으로 갱신한다.
    """
    global rotation_pending
    os.environ[ENV_REFRESH_TOKEN] = new_refresh

    if os.environ.get("GITHUB_ACTIONS") == "true":
        admin_token = os.environ.get(ENV_ADMIN_TOKEN)
        if admin_token and _update_github_secret(ENV_REFRESH_TOKEN, new_refresh, admin_token):
            logger.info("GitHub Secret %s 회전 갱신 완료 (%s)",
                        ENV_REFRESH_TOKEN, mask_status(new_refresh))
        else:
            rotation_pending = True
            logger.warning(
                "refresh_token이 회전됐으나 GitHub Secret %s 자동 갱신 실패/불가 — "
                "수동 갱신 경고를 발송해야 합니다.", ENV_REFRESH_TOKEN
            )
    else:
        update_env_var(ENV_REFRESH_TOKEN, new_refresh, env_path)
        logger.info("새 refresh_token 회전 저장 완료 (%s)", mask_status(new_refresh))


# ── 토큰 갱신 ────────────────────────────────────────────────────────
def refresh_access_token(env_path: str = DEFAULT_ENV_PATH) -> str:
    """refresh_token으로 access token을 갱신해 반환.

    응답에 새 refresh_token이 포함되면(잔여 유효기간 1개월 미만 시 발급)
    .env의 KAKAO_REFRESH_TOKEN을 회전 저장한다.
    """
    file_data = load_env(env_path)
    rest_api_key = get_env_value(ENV_REST_API_KEY, env_path, file_data)
    refresh_token = get_env_value(ENV_REFRESH_TOKEN, env_path, file_data)
    client_secret = get_env_value(ENV_CLIENT_SECRET, env_path, file_data)

    if not rest_api_key:
        raise RuntimeError(f"{ENV_REST_API_KEY} 미설정 — .env를 확인하세요.")
    if not refresh_token:
        raise RuntimeError(
            f"{ENV_REFRESH_TOKEN} 미설정 — 먼저 `py -3 kakao_auth.py`로 인증하세요."
        )

    payload = {
        "grant_type": "refresh_token",
        "client_id": rest_api_key,
        "refresh_token": refresh_token,
    }
    if client_secret:
        payload["client_secret"] = client_secret

    logger.info("access token 갱신 요청 (refresh_token %s)", mask_status(refresh_token))
    try:
        _status, body = _http_request(
            TOKEN_URL, data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        )
    except RuntimeError as e:
        if "invalid_grant" in str(e):
            raise RuntimeError(
                "refresh_token 만료/폐기(invalid_grant) — "
                "`py -3 kakao_auth.py`로 재인증하세요."
            ) from e
        raise

    parsed = json.loads(body)
    access_token = parsed.get("access_token")
    if not access_token:
        raise RuntimeError(f"토큰 응답에 access_token 없음: {parsed.get('error', 'unknown')}")

    new_refresh = parsed.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        # 회전된 토큰의 영속화는 실행 환경(로컬/러너)에 따라 분기 처리한다.
        _persist_rotated_refresh_token(new_refresh, env_path)

    return access_token


# ── 텍스트 분할 ──────────────────────────────────────────────────────
def split_text(text: str, budget: int = CHUNK_BUDGET) -> list:
    """text를 조각 리스트로 분할.

    - 전체가 200자 이하이면 분할하지 않고 [text] 반환(표기 없이 그대로 전송).
    - 초과 시 budget 글자 이하 조각으로 분할. 가독성을 위해 줄바꿈 > 공백 경계를
      우선하고, 적절한 경계가 없으면 하드 컷.
    """
    text = text.rstrip("\n")
    if len(text) <= TEXT_TEMPLATE_MAX:
        return [text]

    chunks: list = []
    remaining = text
    while remaining:
        if len(remaining) <= budget:
            chunks.append(remaining)
            break
        window = remaining[:budget]
        cut = window.rfind("\n")
        if cut < budget // 2:
            cut = window.rfind(" ")
        if cut < budget // 2:
            cut = budget  # 적절한 경계 없음 → 하드 컷
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return chunks


# ── 전송 ─────────────────────────────────────────────────────────────
def send_to_me(text: str, link: Optional[dict] = None,
               env_path: str = DEFAULT_ENV_PATH) -> int:
    """"나에게 보내기"로 text 전송. 200자 초과 시 자동 분할 후 각 조각에 (i/N) 표기.

    access token은 한 번만 갱신해 모든 조각에 재사용한다.
    반환: 전송 성공한 조각 수.
    """
    if not text or not text.strip():
        raise ValueError("빈 메시지는 전송할 수 없습니다.")

    access_token = refresh_access_token(env_path)
    link = link if link is not None else DEFAULT_LINK

    chunks = split_text(text)
    total = len(chunks)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
    }

    sent = 0
    for idx, chunk in enumerate(chunks, start=1):
        piece = f"({idx}/{total})\n{chunk}" if total > 1 else chunk
        piece = piece[:TEXT_TEMPLATE_MAX]  # 200자 하드 가드(Kakao 400 방지)
        template = {
            "object_type": "text",
            "text": piece,
            "link": link,
        }
        data = {"template_object": json.dumps(template, ensure_ascii=False)}
        _status, body = _http_request(MEMO_SEND_URL, data=data, headers=headers)
        result = json.loads(body)
        if result.get("result_code") != 0:
            raise RuntimeError(f"전송 실패 (조각 {idx}/{total}): {body}")
        sent += 1
        logger.info("조각 %d/%d 전송 완료", idx, total)

    logger.info("전체 %d개 조각 전송 완료", sent)
    return sent


# ── CLI (테스트 실행 경로) ───────────────────────────────────────────
def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="카카오 나에게 보내기 전송 모듈")
    parser.add_argument("--test", metavar="MESSAGE",
                        help="지정한 메시지를 나에게 전송(실전송 테스트)")
    parser.add_argument("--env", default=DEFAULT_ENV_PATH, help=".env 경로 (기본 .env)")
    parser.add_argument("--check", action="store_true",
                        help="키 설정 상태만 마스킹 진단(전송 안 함)")
    args = parser.parse_args(argv)

    # CLI 실행 시에만 핸들러 구성(라이브러리 import 시엔 구성하지 않음).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.check:
        fd = load_env(args.env)
        for k in (ENV_REST_API_KEY, ENV_REFRESH_TOKEN, ENV_CLIENT_SECRET):
            print(f"{k}: {mask_status(get_env_value(k, args.env, fd))}")
        return 0

    if args.test:
        try:
            n = send_to_me(args.test, env_path=args.env)
        except Exception as e:  # noqa: BLE001 - CLI 최상위 경계에서 사용자에게 요약 전달
            logger.error("전송 실패: %s", e)
            return 1
        print(f"전송 완료: {n}개 조각")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
