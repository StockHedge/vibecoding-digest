#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect.py — 커뮤니티별 최근 72시간 인기 글 수집기 모음.

지원 타입 (communities.json의 "type" 필드):
  - reddit      : 서브레딧 인기 글. 공개 JSON(top.json) 또는 OAuth2 client_credentials
                  (application-only, .env에 REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET
                  있을 때만) 두 모드를 지원한다.
  - hn_algolia  : Hacker News(Algolia 검색 API) 인기 글.
  - rss         : 임의 RSS 2.0 / Atom 피드(예: GeekNews).
  - dcinside    : 디시인사이드 갤러리 개념글(추천글) 목록.

공통 반환 포맷(수집기 함수는 posts 리스트만, collect_community가 아래로 감싼다):
  {"community_id": str, "community_name": str,
   "posts": [{"title", "url", "score", "score_label", "created_iso"}, ...],
   "error": str | None}

공통 정책:
  - HTTP는 kakao_sender._http_request를 재사용한다(timeout 10s, 4xx 즉시 실패,
    5xx/429/네트워크 오류만 지수 백오프 재시도 2회 — 이미 구현되어 있어 중복 방지).
  - 커뮤니티 단위 실패 격리: collect_community()가 예외를 잡아 error 필드에 담고
    다음 커뮤니티로 진행한다(파이프라인 전체가 한 커뮤니티 실패로 중단되지 않음).
  - 표준 라이브러리만 사용(PyYAML 등 외부 의존성 금지 — communities.json은 JSON).

인코딩 참고:
  - _http_request는 응답 본문을 UTF-8로 디코드한다. 국내 사이트(dcinside 등)는
    과거 EUC-KR을 쓰던 이력이 있어 우려했으나, 실측(2026-07-18, curl로 Content-Type
    헤더 및 원시 바이트 확인) 결과 dcinside·news.hada.io(GeekNews) 모두 현재
    UTF-8을 반환함을 확인했다. 향후 다른 갤러리/피드가 다른 인코딩을 쓰면
    UnicodeDecodeError가 그대로 전파되고, collect_community의 예외 격리가
    이를 잡아 해당 커뮤니티만 실패 처리한다.

로깅 참고:
  - _http_request는 내부적으로 kakao_sender 모듈 전역의 logger(이름:
    "kakao_sender")로 재시도/오류를 남긴다. collect.py에서 그대로 재사용하면
    reddit 수집 중 재시도 로그가 "kakao_sender:" 이름으로 찍혀 카카오 전송과
    혼동된다(실측 지적). kakao_sender.py 파일은 수정 대상이 아니므로, 대신
    _http()/_http_reddit() 래퍼가 호출 구간에서만 그 전역 logger를 collect
    전용 로거로 일시 전환했다가 즉시 복원한다(아래 참고).
"""
from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Optional

import kakao_sender
from kakao_sender import DEFAULT_ENV_PATH, _http_request, get_env_value, load_env

logger = logging.getLogger("collect")
_http_logger = logging.getLogger("collect.http")

# reddit 요청 간 "최소 바닥" 간격(초) — x-ratelimit 헤더가 없을 때만 쓰는 폴백값.
# 실측(2026-07-18, 7개 커뮤니티 리허설 + 재검증): 고정 간격 9s/10s/20s 세 가지
# 모두 동일하게 3번째 reddit 커뮤니티에서 429로 실패했다. 원인을 curl로 직접
# 확인한 결과, reddit은 응답에 x-ratelimit-remaining/x-ratelimit-reset 헤더로
# "이 IP의 남은 요청 수/리셋까지 초"를 정확히 알려주고 있었다(예:
# remaining=0.0, reset=25 — 어떤 서브레딧을 요청했는지와 무관하게 IP 단위로
# 공유되는 버킷임을 확인). 고정 간격은 이 실제 버킷 상태와 우연히
# 맞아떨어질 때만 통과하므로 근본 해결이 아니다 — 아래 _reddit_fetch()가 이
# 헤더를 직접 읽어 적응형으로 대기한다. 이 값은 헤더가 없는 응답(예: .json의
# 영구 WAF 403 차단 — 재시도로 풀리지 않는 별개 현상)에 대한 최소 폴백 간격이다.
REDDIT_REQUEST_INTERVAL = 10.0
# x-ratelimit-reset 헤더 값에 더하는 여유(초). 헤더가 알려주는 시각에 정확히
# 맞춰 재요청하면 경계값 오차로 다시 걸릴 수 있어 약간 더 기다린다.
REDDIT_RATELIMIT_MARGIN = 2.0

KST = timezone(timedelta(hours=9))
WINDOW_HOURS = 72  # 수집 창(스펙: 최근 72시간 인기 글)

# Reddit API 가이드라인 권장 형식: <platform>:<app id>:<version> (사용 목적).
REDDIT_USER_AGENT = "windows:vibecoding-digest:v1.0 (personal use, 1 req/3days)"
# 그 외 수집기(HN Algolia/RSS/dcinside)용 일반 User-Agent.
GENERIC_USER_AGENT = "Mozilla/5.0 (compatible; vibecoding-digest/1.0; personal use)"

REDDIT_ENV_CLIENT_ID = "REDDIT_CLIENT_ID"
REDDIT_ENV_CLIENT_SECRET = "REDDIT_CLIENT_SECRET"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

HN_ALGOLIA_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
HN_ALGOLIA_SEARCH_BY_DATE_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_HITS_PER_PAGE = 100

ATOM_NS = "{http://www.w3.org/2005/Atom}"

DCINSIDE_LIST_URL_TMPL = (
    "https://gall.dcinside.com/{board_type}/board/lists/"
    "?id={gallery_id}&exception_mode=recommend"
)


# ── 공통 유틸 ────────────────────────────────────────────────────────
def _http(url: str, **kwargs):
    """kakao_sender._http_request 래퍼(모든 수집기 공용).

    호출 구간에서만 kakao_sender 모듈의 전역 logger를 collect 전용 로거로
    바꿔치기했다가 finally에서 복원한다 — kakao_sender.py 파일은 그대로 두고도
    "kakao_sender:" 이름으로 찍히던 재시도 로그를 "collect.http:"로 구분되게
    한다. 단일 프로세스 순차 호출을 전제하므로(이 파이프라인의 기존 설계와 동일)
    스레드 안전성은 고려하지 않는다.
    """
    original = kakao_sender.logger
    kakao_sender.logger = _http_logger
    try:
        return _http_request(url, **kwargs)
    finally:
        kakao_sender.logger = original


_last_reddit_request_at: Optional[float] = None
# x-ratelimit 헤더로 계산한 "이 시각까지는 reddit에 재요청하지 말 것"(monotonic).
_reddit_wait_until: Optional[float] = None


def _reddit_note_ratelimit(resp_headers: dict) -> bool:
    """응답의 x-ratelimit-remaining/x-ratelimit-reset을 읽어 다음 요청 대기 시각을 갱신.

    remaining이 1 미만(사실상 소진)이면 reset(초) + 여유분만큼 뒤를
    "다음 요청 가능 시각"으로 기록한다. 헤더가 없거나(예: .json의 WAF 403은
    이 헤더 자체가 없다) 파싱할 수 없으면 아무것도 하지 않는다(폴백은
    _reddit_throttle의 REDDIT_REQUEST_INTERVAL이 담당).

    반환값: 이 응답에서 유효한 x-ratelimit 헤더를 읽어낼 수 있었으면 True.
    """
    global _reddit_wait_until
    if not resp_headers:
        return False
    lowered = {k.lower(): v for k, v in resp_headers.items()}
    remaining = lowered.get("x-ratelimit-remaining")
    reset = lowered.get("x-ratelimit-reset")
    if remaining is None or reset is None:
        return False
    try:
        remaining_f = float(remaining)
        reset_f = float(reset)
    except (TypeError, ValueError):
        return False
    if remaining_f < 1:
        candidate = time.monotonic() + reset_f + REDDIT_RATELIMIT_MARGIN
        if _reddit_wait_until is None or candidate > _reddit_wait_until:
            _reddit_wait_until = candidate
        logger.info("reddit 레이트리밋 소진 감지(remaining=%s) — 약 %.1f초 후 재요청 가능",
                    remaining, reset_f + REDDIT_RATELIMIT_MARGIN)
    return True


def _reddit_throttle() -> None:
    """다음 reddit 요청 전 대기. x-ratelimit 기반 대기 시각이 있으면 그것을,
    없으면 REDDIT_REQUEST_INTERVAL 고정 간격을 사용(둘 중 더 늦은 시점까지)."""
    waits = []
    if _reddit_wait_until is not None:
        waits.append(_reddit_wait_until - time.monotonic())
    if _last_reddit_request_at is not None:
        waits.append(REDDIT_REQUEST_INTERVAL - (time.monotonic() - _last_reddit_request_at))
    wait = max(waits) if waits else 0
    if wait > 0:
        logger.info("reddit 요청 간격 확보를 위해 %.1f초 대기", wait)
        time.sleep(wait)


def _reddit_fetch(url: str, data: Optional[dict] = None, headers: Optional[dict] = None,
                  timeout: float = kakao_sender.HTTP_TIMEOUT,
                  retries: int = kakao_sender.HTTP_RETRIES) -> tuple:
    """reddit 전용 HTTP GET/POST. kakao_sender._http_request와 재시도 정책은
    동일하되(timeout 10s, 4xx 즉시 실패, 5xx/429/네트워크 오류만 재시도) 응답
    헤더까지 반환한다는 점이 다르다.

    kakao_sender._http_request는 (status, body)만 반환해 헤더를 버리는데,
    reddit의 x-ratelimit-remaining/x-ratelimit-reset 헤더 없이는 정확한 재요청
    시점을 알 수 없다(고정 간격만으로는 재현되는 429를 실측으로 확인함 — 위
    REDDIT_REQUEST_INTERVAL 주석 참고). kakao_sender.py 파일 자체는 수정하지
    않고, collect.py 안에서만 이 별도 구현을 둔다.
    """
    body_enc = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    attempt = 0
    while True:
        try:
            req = urllib.request.Request(url, data=body_enc, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_headers = dict(resp.headers)
                _reddit_note_ratelimit(resp_headers)
                return resp.status, resp.read().decode("utf-8"), resp_headers
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            resp_headers = dict(e.headers) if e.headers else {}
            is_ratelimit = e.code == 429 and _reddit_note_ratelimit(resp_headers)
            transient = e.code == 429 or 500 <= e.code < 600
            if not transient or attempt >= retries:
                snippet = err_body[:300] + ("…" if len(err_body) > 300 else "")
                raise RuntimeError(f"HTTP {e.code}: {snippet}") from e
            _http_logger.warning("HTTP %s 일시 오류(reddit), 재시도 %d/%d",
                                 e.code, attempt + 1, retries)
            attempt += 1
            if is_ratelimit:
                _reddit_throttle()  # x-ratelimit 헤더가 알려준 리셋 시각까지 대기
            else:
                time.sleep(REDDIT_RATELIMIT_MARGIN * (2 ** attempt))  # 5xx는 일반 백오프
            continue
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt >= retries:
                raise RuntimeError(f"네트워크 오류: {e}") from e
            _http_logger.warning("네트워크 오류(%s), 재시도 %d/%d", e, attempt + 1, retries)
            attempt += 1
            time.sleep(REDDIT_RATELIMIT_MARGIN * (2 ** attempt))  # 네트워크 오류도 일반 백오프
            continue


def _http_reddit(url: str, **kwargs):
    """reddit 전용 HTTP 래퍼: x-ratelimit 헤더 기반 적응형 스로틀링 + 로거 전환.

    OAuth 토큰 발급, OAuth API 조회, 공개 JSON, 공개 RSS 등 reddit으로 나가는
    모든 요청이 이 래퍼를 거친다(한 커뮤니티 내 json→rss 폴백 사이, 그리고
    커뮤니티 간 모두 적용). _http_request가 아닌 _reddit_fetch를 써서 응답
    헤더를 확보한다.
    """
    global _last_reddit_request_at
    _reddit_throttle()
    original = kakao_sender.logger
    kakao_sender.logger = _http_logger
    try:
        status, body, _headers = _reddit_fetch(url, **kwargs)
        return status, body
    finally:
        kakao_sender.logger = original
        _last_reddit_request_at = time.monotonic()


def _cutoff_utc() -> datetime:
    """72시간 수집 창의 시작 시각(UTC, timezone-aware)."""
    return datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)


def _to_kst_iso(dt: datetime) -> str:
    """timezone-aware datetime을 KST ISO8601 문자열로 변환(naive는 UTC로 간주)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).isoformat(timespec="seconds")


def _parse_feed_datetime(value: str) -> datetime:
    """RSS pubDate(RFC822) 또는 Atom published/updated(ISO8601)를 datetime으로 변환."""
    value = value.strip()
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        if value.endswith("Z"):
            return datetime.fromisoformat(value[:-1] + "+00:00")
        raise


# ── reddit ───────────────────────────────────────────────────────────
def _reddit_oauth_token(client_id: str, client_secret: str) -> str:
    """OAuth2 client_credentials(application-only)로 access token 발급."""
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("ascii")).decode("ascii")
    headers = {
        "Authorization": f"Basic {basic}",
        "User-Agent": REDDIT_USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    _status, body = _http_reddit(
        REDDIT_TOKEN_URL, data={"grant_type": "client_credentials"}, headers=headers
    )
    parsed = json.loads(body)
    token = parsed.get("access_token")
    if not token:
        raise RuntimeError(f"Reddit OAuth 토큰 발급 실패: {parsed.get('error', 'unknown')}")
    return token


def _reddit_posts_from_children(children: list, cutoff_epoch: float) -> list:
    posts = []
    for child in children:
        d = child.get("data", {})
        created = d.get("created_utc")
        if created is None or created < cutoff_epoch:
            continue
        score = int(d.get("score", 0))
        permalink = d.get("permalink", "")
        url = f"https://www.reddit.com{permalink}" if permalink else d.get("url", "")
        posts.append({
            "title": d.get("title") or "(제목 없음)",
            "url": url,
            "score": score,
            "score_label": f"추천 {score}",
            "created_iso": _to_kst_iso(datetime.fromtimestamp(created, tz=timezone.utc)),
        })
    return posts


def _short(e: Exception, limit: int = 50) -> str:
    """예외 메시지를 한 줄로 축약(HTTP 오류 응답 본문 등 장문 방지)."""
    msg = " ".join(str(e).split())
    return msg if len(msg) <= limit else msg[:limit] + "…"


def _collect_reddit_oauth(subreddit: str, limit: int, cutoff_epoch: float,
                          client_id: str, client_secret: str) -> list:
    """OAuth2 client_credentials 모드(oauth.reddit.com) — 자격증명이 있을 때의 주 경로.

    공개 엔드포인트 대비 안정적이고 score를 포함하므로, 자격증명이 있으면 항상
    이 경로를 우선한다(실측, 2026-07-18: 이 샌드박스 환경에서는 자격증명 없이
    oauth.reddit.com에 접근해도 공개 페이지와 동일한 WAF 403을 받았다 — 즉
    OAuth 모드가 통하려면 실제 유효한 access token이 전제되어야 한다).
    """
    token = _reddit_oauth_token(client_id, client_secret)
    url = f"https://oauth.reddit.com/r/{subreddit}/top?t=week&limit={limit}&raw_json=1"
    headers = {"Authorization": f"Bearer {token}", "User-Agent": REDDIT_USER_AGENT}
    logger.info("reddit r/%s: OAuth 모드로 수집(주 경로)", subreddit)
    _status, body = _http_reddit(url, headers=headers)
    parsed = json.loads(body)
    children = parsed.get("data", {}).get("children", [])
    posts = _reddit_posts_from_children(children, cutoff_epoch)
    posts.sort(key=lambda p: p["score"], reverse=True)
    return posts


def _collect_reddit_public_json(subreddit: str, limit: int, cutoff_epoch: float) -> list:
    """공개 JSON(top.json) — 자격증명이 없을 때 1차 시도.

    실측(2026-07-18): 이 샌드박스 환경에서는 www.reddit.com/old.reddit.com 모두
    reddit 권장 형식 User-Agent를 붙여도 HTTP 403(WAF 차단)이었다. 데이터센터
    IP 대역 차단으로 추정되며, 일반 가정 네트워크에서는 성공할 수 있다.
    """
    url = f"https://www.reddit.com/r/{subreddit}/top.json?t=week&limit={limit}&raw_json=1"
    headers = {"User-Agent": REDDIT_USER_AGENT}
    logger.info("reddit r/%s: 공개 JSON 모드 시도", subreddit)
    _status, body = _http_reddit(url, headers=headers)
    parsed = json.loads(body)
    children = parsed.get("data", {}).get("children", [])
    posts = _reddit_posts_from_children(children, cutoff_epoch)
    posts.sort(key=lambda p: p["score"], reverse=True)
    return posts


def _collect_reddit_public_rss(subreddit: str, cutoff_epoch: float) -> list:
    """공개 RSS 변형(top/.rss) — .json이 막혔을 때 2차 시도.

    실측(2026-07-18): 이 샌드박스 환경에서 .json은 403이었지만
    https://www.reddit.com/r/<sub>/top/.rss?t=week 는 200 OK로 실제 r/ClaudeCode
    게시물(Atom 피드, entry/title/link/updated)을 반환했다. 단, 이 피드는 score
    수치를 제공하지 않으므로 score=0/score_label="-"로 채우고 reddit이 이미
    top(week) 정렬해 준 피드 순서를 그대로 유지한다(재정렬하지 않음).
    """
    url = f"https://www.reddit.com/r/{subreddit}/top/.rss?t=week"
    headers = {"User-Agent": REDDIT_USER_AGENT}
    logger.info("reddit r/%s: 공개 RSS 변형 모드 시도", subreddit)
    _status, body = _http_reddit(url, headers=headers)
    cutoff_dt = datetime.fromtimestamp(cutoff_epoch, tz=timezone.utc)
    root = ET.fromstring(body)
    return _atom_posts(root, cutoff_dt)


def collect_reddit(params: dict, env_path: str = DEFAULT_ENV_PATH) -> list:
    """서브레딧 인기 글(최근 week 상위) 중 72시간 이내 글만 반환.

    우선순위: ① .env에 REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET이 모두 있으면
    OAuth2 client_credentials 모드(주 경로, score 포함) → ② 없으면 공개 JSON
    (top.json) 시도 → ③ 그마저 실패하면 공개 RSS 변형(top/.rss) 시도.
    ②·③이 모두 실패하면 조용히 빈 결과를 반환하는 대신, 자격증명 설정이
    필요하다는 것을 명확히 담은 예외를 던져 report.py의 [수집 실패] 섹션에
    그대로 드러나게 한다.
    """
    subreddit = params.get("subreddit")
    if not subreddit:
        raise ValueError("params.subreddit 누락")
    limit = int(params.get("limit", 50))
    cutoff_epoch = _cutoff_utc().timestamp()

    file_data = load_env(env_path)
    client_id = get_env_value(REDDIT_ENV_CLIENT_ID, env_path, file_data)
    client_secret = get_env_value(REDDIT_ENV_CLIENT_SECRET, env_path, file_data)

    if client_id and client_secret:
        return _collect_reddit_oauth(subreddit, limit, cutoff_epoch, client_id, client_secret)

    try:
        return _collect_reddit_public_json(subreddit, limit, cutoff_epoch)
    except Exception as e_json:
        logger.info("reddit r/%s: 공개 JSON 실패(%s) → RSS 변형 재시도",
                    subreddit, _short(e_json))
        try:
            return _collect_reddit_public_rss(subreddit, cutoff_epoch)
        except Exception as e_rss:
            raise RuntimeError(
                "Reddit 공개 API 차단(.json/.rss 모두 실패) — "
                "REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET 설정 필요 "
                f"[json: {_short(e_json)}] [rss: {_short(e_rss)}]"
            ) from e_rss


# ── hn_algolia ───────────────────────────────────────────────────────
def collect_hn_algolia(params: dict) -> list:
    """HN Algolia 검색 결과 중 72시간 이내 글만 반환, points 내림차순.

    query가 있으면 관련도 검색(/search)을, 없으면 태그 전용 시간순 검색
    (/search_by_date)을 사용해 72시간 창을 빠짐없이 커버한다. 두 엔드포인트
    모두 응답이 points로 정렬되어 있지 않으므로(실측 확인, 2026-07-18) 항상
    클라이언트에서 points 기준으로 재정렬한다.

    주의: 응답의 nbHits는 Algolia 색인의 근사치 카운트라 신뢰할 수 없다
    (실측 확인 — 페이지네이션 총량 표시용일 뿐 실제 매칭 건수와 다를 수 있음).
    따라서 nbHits는 어디서도 참조하지 않고, 오직 hits 배열(실제 반환된 문서)만
    사용한다.
    """
    query = (params.get("query") or "").strip()
    cutoff_epoch = int(_cutoff_utc().timestamp())

    base_url = HN_ALGOLIA_SEARCH_URL if query else HN_ALGOLIA_SEARCH_BY_DATE_URL
    query_params = {
        "tags": "story",
        "numericFilters": f"created_at_i>{cutoff_epoch}",
        "hitsPerPage": str(HN_HITS_PER_PAGE),
    }
    if query:
        query_params["query"] = query
    url = f"{base_url}?{urllib.parse.urlencode(query_params)}"

    headers = {"User-Agent": GENERIC_USER_AGENT}
    _status, body = _http(url, headers=headers)
    parsed = json.loads(body)

    posts = []
    for hit in parsed.get("hits", []):  # nbHits는 사용하지 않는다(근사치, 신뢰 불가)
        created_i = hit.get("created_at_i")
        if created_i is None or created_i < cutoff_epoch:
            continue  # numericFilters로 이미 걸러지지만 이중 안전장치
        points = int(hit.get("points") or 0)
        object_id = hit.get("objectID")
        post_url = hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}"
        title = hit.get("title") or hit.get("story_title") or "(제목 없음)"
        posts.append({
            "title": title,
            "url": post_url,
            "score": points,
            "score_label": f"포인트 {points}",
            "created_iso": _to_kst_iso(datetime.fromtimestamp(created_i, tz=timezone.utc)),
        })
    posts.sort(key=lambda p: p["score"], reverse=True)
    return posts


# ── rss (RSS 2.0 / Atom 겸용) ────────────────────────────────────────
def _rss2_posts(channel: ET.Element, cutoff: datetime) -> list:
    posts = []
    for item in channel.findall("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        pub_el = item.find("pubDate")
        title = (title_el.text or "").strip() if title_el is not None else "(제목 없음)"
        link = (link_el.text or "").strip() if link_el is not None else ""
        pub_text = pub_el.text if pub_el is not None else None
        if not pub_text:
            continue
        dt = _parse_feed_datetime(pub_text)
        if dt < cutoff:
            continue
        posts.append({
            "title": title, "url": link, "score": 0, "score_label": "-",
            "created_iso": _to_kst_iso(dt),
        })
    return posts


def _atom_posts(root: ET.Element, cutoff: datetime) -> list:
    posts = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        title_el = entry.find(f"{ATOM_NS}title")
        title = (title_el.text or "").strip() if title_el is not None else "(제목 없음)"
        link_el = entry.find(f"{ATOM_NS}link[@rel='alternate']")
        if link_el is None:
            link_el = entry.find(f"{ATOM_NS}link")
        link = link_el.get("href", "") if link_el is not None else ""
        pub_el = entry.find(f"{ATOM_NS}published")
        if pub_el is None:
            pub_el = entry.find(f"{ATOM_NS}updated")
        pub_text = pub_el.text if pub_el is not None else None
        if not pub_text:
            continue
        dt = _parse_feed_datetime(pub_text)
        if dt < cutoff:
            continue
        posts.append({
            "title": title, "url": link, "score": 0, "score_label": "-",
            "created_iso": _to_kst_iso(dt),
        })
    return posts


def collect_rss(params: dict) -> list:
    """임의 RSS 2.0 / Atom 피드에서 72시간 이내 글만 반환.

    점수 개념이 없으므로 score=0/score_label="-"로 채우고, 피드가 준 원래 순서
    (최신순인 경우가 대부분)를 유지한다.

    실측(2026-07-18, WebFetch+curl로 확인): GeekNews(news.hada.io)의 실제 RSS 경로는
    https://news.hada.io/rss/news 이며, 형식은 RSS 2.0이 아니라 **Atom**
    (루트 엘리먼트 <feed>, 날짜 필드는 pubDate가 아닌 published/updated)이다.
    User-Agent는 브라우저형이든 일반 문자열이든 무관하게 200 OK였다(UA가 원인이
    아니다).

    함정 주의: 슬래시 없는 https://news.hada.io/rss 는 curl -L 기준
    https://news.hada.io/rss/ 로 301 리다이렉트되고, 그 리다이렉트 목적지가
    403으로 차단된다(UA와 무관). /rss/news(뒤에 "/news"가 붙은 전체 경로)만
    실제로 200을 반환하므로, communities.json의 feed_url은 반드시 이 전체
    경로를 그대로 사용해야 한다 — "/rss"만 쓰면 다른(차단된) 경로로 샌다.
    """
    feed_url = params.get("feed_url")
    if not feed_url:
        raise ValueError("params.feed_url 누락")

    headers = {"User-Agent": GENERIC_USER_AGENT}
    _status, body = _http(feed_url, headers=headers)
    cutoff = _cutoff_utc()

    root = ET.fromstring(body)
    if root.tag.endswith("feed"):
        posts = _atom_posts(root, cutoff)
    elif root.tag == "rss":
        channel = root.find("channel")
        posts = _rss2_posts(channel, cutoff) if channel is not None else []
    else:
        raise ValueError(f"지원하지 않는 피드 루트 엘리먼트: {root.tag}")
    return posts


# ── dcinside ─────────────────────────────────────────────────────────
class _DcInsideRowParser(HTMLParser):
    """dcinside 갤러리 리스트(exception_mode=recommend) HTML에서 행 단위로 파싱.

    실측(2026-07-18, id=chatgpt 마이너 갤러리) DOM 구조:
      <tr class="ub-content us-post" data-no="...">
        <td class="gall_tit ub-word"><a href="/board/view/?...">제목</a>...</td>
        <td class="gall_date" title="2026-07-15 12:34:56">26.07.15</td>
        <td class="gall_recommend">27</td>
      </tr>
    공지/광고 등 특수 행은 <tr>에 data-type 속성(icon_notice 등)이 붙어 있어 제외한다.
    galery 종류에 따라 gall_subject 등 추가 컬럼 유무가 다를 수 있으나, 클래스명
    매칭 방식이라 컬럼 순서/개수 차이에 영향받지 않는다.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list = []
        self._in_row = False
        self._skip_row = False
        self._cur: Optional[dict] = None
        self._field: Optional[str] = None
        self._got_title_link = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attrs_d = dict(attrs)
        classes = (attrs_d.get("class") or "").split()

        if tag == "tr" and "ub-content" in classes:
            self._in_row = True
            self._skip_row = "data-type" in attrs_d  # 공지/광고 등 특수 행 제외
            self._cur = {"title": None, "url": None, "recommend": None, "date": None}
            self._got_title_link = False
            return

        if not self._in_row or self._skip_row:
            return

        if tag == "td":
            if "gall_tit" in classes:
                self._field = "tit"
            elif "gall_recommend" in classes:
                self._field = "recommend"
            elif "gall_date" in classes:
                self._field = "date"
                self._cur["date"] = attrs_d.get("title")  # 전체 일시는 title 속성에 있음
            else:
                self._field = None
        elif tag == "a" and self._field == "tit" and not self._got_title_link:
            href = attrs_d.get("href", "")
            if href.startswith("/"):
                href = "https://gall.dcinside.com" + href
            self._cur["url"] = href
            self._got_title_link = True  # 댓글수 배지(reply_numbox) 등 이후 <a>는 무시

    def handle_data(self, data: str) -> None:
        if not self._in_row or self._skip_row or self._field is None or self._cur is None:
            return
        data = data.strip()
        if not data:
            return
        if self._field == "tit" and self._got_title_link and self._cur["title"] is None:
            self._cur["title"] = data
        elif self._field == "recommend" and self._cur["recommend"] is None:
            self._cur["recommend"] = data

    def handle_endtag(self, tag: str) -> None:
        if tag == "td":
            self._field = None
        elif tag == "tr" and self._in_row:
            if not self._skip_row and self._cur and self._cur["title"] and self._cur["url"]:
                self.rows.append(self._cur)
            self._in_row = False
            self._skip_row = False
            self._cur = None


def collect_dcinside(params: dict) -> list:
    """디시인사이드 갤러리 개념글(추천글) 목록 중 72시간 이내 글만 반환, 추천수 내림차순.

    주의(placeholder 상태, config에서 enabled=false로 유지 중): 목록 페이지에는
    상대적 날짜만 표기되고 정확한 일시는 gall_date의 title 속성에서만 얻을 수
    있으며, DOM 클래스명은 갤러리 개편 시 바뀔 수 있다. 실사용 전 실제 응답으로
    셀렉터 재검증을 권장한다.
    """
    gallery_id = params.get("gallery_id")
    if not gallery_id:
        raise ValueError("params.gallery_id 누락")
    board_type = params.get("board_type", "mgallery")  # 마이너 갤러리 기본, 정식은 "board"

    url = DCINSIDE_LIST_URL_TMPL.format(board_type=board_type, gallery_id=gallery_id)
    headers = {"User-Agent": GENERIC_USER_AGENT}
    _status, body = _http(url, headers=headers)

    parser = _DcInsideRowParser()
    parser.feed(body)

    cutoff = datetime.now(KST) - timedelta(hours=WINDOW_HOURS)
    posts = []
    for row in parser.rows:
        dt = None
        date_str = row.get("date")
        if date_str:
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
            except ValueError:
                dt = None
        if dt is not None and dt < cutoff:
            continue
        try:
            recommend = int(row.get("recommend") or 0)
        except ValueError:
            recommend = 0
        posts.append({
            "title": row["title"],
            "url": row["url"],
            "score": recommend,
            "score_label": f"추천 {recommend}",
            "created_iso": dt.isoformat(timespec="seconds") if dt else "",
        })
    posts.sort(key=lambda p: p["score"], reverse=True)
    return posts


# ── 디스패치 & 커뮤니티 단위 실패 격리 ─────────────────────────────────
_COLLECTORS = {
    "reddit": collect_reddit,
    "hn_algolia": collect_hn_algolia,
    "rss": collect_rss,
    "dcinside": collect_dcinside,
}


def collect_community(community: dict, env_path: str = DEFAULT_ENV_PATH) -> dict:
    """커뮤니티 1건 수집. 실패해도 예외를 던지지 않고 error 필드에 담아 반환한다."""
    community_id = community.get("id", "?")
    name = community.get("name", community_id)
    ctype = community.get("type")
    params = community.get("params", {})

    fn = _COLLECTORS.get(ctype)
    if fn is None:
        msg = f"알 수 없는 수집기 타입: {ctype}"
        logger.warning("%s: %s", name, msg)
        return {"community_id": community_id, "community_name": name, "posts": [], "error": msg}

    try:
        posts = fn(params, env_path=env_path) if ctype == "reddit" else fn(params)
        logger.info("%s(%s): %d건 수집", name, ctype, len(posts))
        return {"community_id": community_id, "community_name": name, "posts": posts, "error": None}
    except Exception as e:  # noqa: BLE001 - 커뮤니티 단위 실패 격리(스펙 요구사항)
        logger.warning("%s(%s) 수집 실패: %s", name, ctype, e)
        return {"community_id": community_id, "community_name": name, "posts": [], "error": str(e)}


def collect_all(communities: list, env_path: str = DEFAULT_ENV_PATH) -> list:
    """enabled=true인 커뮤니티 전체를 순회 수집(한 커뮤니티 실패가 나머지를 막지 않음)."""
    results = []
    for community in communities:
        if not community.get("enabled", True):
            logger.info("%s: enabled=false, 스킵", community.get("name", community.get("id")))
            continue
        results.append(collect_community(community, env_path=env_path))
    return results
