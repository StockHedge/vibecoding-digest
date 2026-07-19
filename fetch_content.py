#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_content.py — 수집된 각 글의 본문(원문 텍스트)을 확보해 "content" 필드를 얹는다.

collect.collect_all()이 반환한 결과 구조(리스트의 각 원소는
{"community_id", "community_name", "posts": [...], "error"})를 그대로 받아,
error가 없는 커뮤니티의 posts 각 항목에 "content"(str | None) 키를 추가한다
(in-place 수정 — collect.py 자체는 건드리지 않는다).

글 단위 실패 격리: 개별 글의 본문 확보가 실패해도 예외를 던지지 않고 content=None으로
남긴다. summarize.py는 content가 없으면 제목만으로 요약을 시도하거나 "요약 실패"로
표시하므로, 파이프라인 전체가 한 글의 네트워크 오류로 중단되지 않는다.

커뮤니티 타입별 확보 전략과 근거:
  - reddit
      collect.py가 이미 실측 확인한 대로 reddit은 IP 단위로 공유되는 레이트리밋이
      빡빡하다(collect.py의 REDDIT_REQUEST_INTERVAL 주석 참고). 글마다 별도 요청을
      보내면 예산을 곧바로 소진하므로, 서브레딧당 top/.rss?t=week Atom 피드를 딱
      1회(collect._http_reddit로 기존 스로틀링·로거 전환 재사용) 요청해 각 entry의
      <content>(HTML — 자체 텍스트 글은 selftext, 링크 글은 미리보기)를 permalink로
      매칭한다. 매칭되지 않는 글(피드에 없는 하위권 글 등)은 content=None으로 남는다.
  - rss, news.hada.io(GeekNews) 도메인 한정
      글의 url(topic 페이지)을 fetch해 <div id="topic_contents"> 내부 텍스트를
      추출한다. GeekNews 편집자가 이미 한국어로 정리한 요약이라 원문 링크(대개 외부
      영어 사이트) 없이도 한국어 콘텐츠를 확보할 수 있다.
  - rss(그 외, 예: Lobsters) / hn_algolia
      글의 url이 가리키는 대상 페이지를 fetch해 <p> 태그 위주로 텍스트를 근사
      추출한다(표준 라이브러리 html.parser 한계상 본문 영역을 정확히 식별하지 못하고
      메뉴/사이드바의 <p>까지 섞일 수 있으나, summarize.py 프롬프트가 이를 감안해
      요약하도록 구성돼 있다). fetch 자체가 실패하면(403/타임아웃/비HTML 등)
      제목만으로 진행한다.
  - 그 외 타입(dcinside 등)
      본문 확보 전략 미정의 — content=None으로 둔다(dcinside는 현재
      communities.json에서 enabled=false).

표준 라이브러리만 사용(html.parser, xml.etree.ElementTree).
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

import collect

logger = logging.getLogger("fetch_content")

MAX_CONTENT_CHARS = 6000
GEEKNEWS_DOMAIN = "news.hada.io"


# ── HTML → 텍스트 추출 ──────────────────────────────────────────────
def _normalize_ws(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _truncate(text: str, limit: int = MAX_CONTENT_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


class _ParagraphTextExtractor(HTMLParser):
    """<p> 태그 내부 텍스트만 순서대로 모아 본문을 근사 추출한다.

    reddit Atom <content>(예: <div class="md"><p>...</p></div> 뒤에 "submitted by
    ... [link] [comments]" 꼬리말이 붙는 구조 — 실측 확인, 2026-07-19)와 HN/Lobsters
    대상 페이지 양쪽에 재사용한다. 꼬리말·내비게이션은 보통 <p> 밖에 있어 자연히
    제외된다.
    """

    _SKIP_TAGS = frozenset({"script", "style", "nav", "header", "footer", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list = []
        self._skip_depth = 0
        self._p_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "p":
            self._p_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "p" and self._p_depth > 0:
            self._p_depth -= 1
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and self._p_depth > 0:
            self._parts.append(data)

    def text(self) -> str:
        return _normalize_ws("".join(self._parts))


class _DivByIdTextExtractor(HTMLParser):
    """<div id="target_id"> ... </div> 내부 텍스트만 추출(div 중첩 깊이 추적).

    GeekNews topic 페이지의 <div id='topic_contents'>는 <ul>/<li>/<h2> 등 비-<p>
    마크업으로 구성돼 _ParagraphTextExtractor로는 잡히지 않아 별도로 둔다.
    """

    _BREAK_TAGS = frozenset({"li", "h2", "h3", "p", "br"})

    def __init__(self, target_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self._target_id = target_id
        self._parts: list = []
        self._capturing = False
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attrs_d = dict(attrs)
        if not self._capturing:
            if tag == "div" and attrs_d.get("id") == self._target_id:
                self._capturing = True
                self._depth = 1
            return
        if tag == "div":
            self._depth += 1
        elif tag in self._BREAK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._capturing and tag == "div":
            self._depth -= 1
            if self._depth == 0:
                self._capturing = False

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)

    def text(self) -> str:
        return _normalize_ws("".join(self._parts))


def _extract_paragraphs(html_fragment: str) -> str:
    parser = _ParagraphTextExtractor()
    parser.feed(html_fragment)
    return parser.text()


def _extract_div_by_id(html_doc: str, target_id: str) -> str:
    parser = _DivByIdTextExtractor(target_id)
    parser.feed(html_doc)
    return parser.text()


# ── reddit ───────────────────────────────────────────────────────────
def _reddit_feed_content_map(subreddit: str) -> dict:
    """서브레딧의 top/.rss?t=week Atom 피드에서 permalink → 본문 텍스트 매핑을 만든다.

    collect._collect_reddit_public_rss와 동일한 URL·스로틀링(collect._http_reddit)을
    재사용하되, 거기서는 버려지는 <content> 필드를 추출한다는 점이 다르다.
    """
    url = f"https://www.reddit.com/r/{subreddit}/top/.rss?t=week"
    headers = {"User-Agent": collect.REDDIT_USER_AGENT}
    _status, body = collect._http_reddit(url, headers=headers)
    root = ET.fromstring(body)
    out = {}
    for entry in root.findall(f"{collect.ATOM_NS}entry"):
        link_el = entry.find(f"{collect.ATOM_NS}link")
        content_el = entry.find(f"{collect.ATOM_NS}content")
        if link_el is None or content_el is None or not content_el.text:
            continue
        permalink = (link_el.get("href") or "").rstrip("/")
        if not permalink:
            continue
        text = _extract_paragraphs(content_el.text)
        if text:
            out[permalink] = text
    return out


def _fetch_reddit_contents(posts: list, params: dict) -> None:
    subreddit = params.get("subreddit")
    if not subreddit:
        for p in posts:
            p["content"] = None
        return
    try:
        content_map = _reddit_feed_content_map(subreddit)
    except Exception as e:  # noqa: BLE001 - 본문 확보 실패가 커뮤니티 전체를 막지 않음
        logger.warning("reddit r/%s: 본문 피드 조회 실패(%s) — 전체 글 제목만으로 진행",
                       subreddit, e)
        for p in posts:
            p["content"] = None
        return

    matched = 0
    for p in posts:
        key = (p.get("url") or "").rstrip("/")
        text = content_map.get(key)
        p["content"] = _truncate(text) if text else None
        if text:
            matched += 1
    logger.info("reddit r/%s: 본문 %d/%d건 확보", subreddit, matched, len(posts))


# ── GeekNews(news.hada.io) ───────────────────────────────────────────
def _fetch_geeknews_contents(posts: list) -> None:
    matched = 0
    for p in posts:
        url = p.get("url")
        if not url:
            p["content"] = None
            continue
        try:
            _status, body = collect._http(url, headers={"User-Agent": collect.GENERIC_USER_AGENT})
            text = _extract_div_by_id(body, "topic_contents")
            p["content"] = _truncate(text) if text else None
            if text:
                matched += 1
        except Exception as e:  # noqa: BLE001 - 글 단위 실패 격리
            logger.info("GeekNews 본문 확보 실패(제목만으로 진행, %s): %s", url, e)
            p["content"] = None
    logger.info("GeekNews: 본문 %d/%d건 확보", matched, len(posts))


# ── HN/Lobsters 등 외부 링크 대상 페이지 ──────────────────────────────
def _fetch_generic_link_contents(posts: list) -> None:
    matched = 0
    for p in posts:
        url = p.get("url")
        if not url:
            p["content"] = None
            continue
        try:
            _status, body = collect._http(url, headers={"User-Agent": collect.GENERIC_USER_AGENT})
            text = _extract_paragraphs(body)
            p["content"] = _truncate(text) if text else None
            if text:
                matched += 1
        except Exception as e:  # noqa: BLE001 - 글 단위 실패 격리(대상 사이트 차단/타임아웃 등)
            logger.info("대상 페이지 본문 확보 실패(제목만으로 진행, %s): %s", url, e)
            p["content"] = None
    logger.info("외부 링크: 본문 %d/%d건 확보", matched, len(posts))


# ── 디스패치 ─────────────────────────────────────────────────────────
def fetch_content_for_results(results: list, communities: list) -> None:
    """collect_all() 반환값의 각 post 딕셔너리에 "content" 필드를 추가(in-place).

    커뮤니티 단위로 실패가 격리되도록 각 분기 함수를 감싼다(이중 안전장치 — 분기
    함수 내부도 이미 글 단위로 격리하지만, 예상 못 한 예외가 커뮤니티 전체를
    막지 않도록 한다).
    """
    community_by_id = {c.get("id"): c for c in communities}
    for r in results:
        if r.get("error") or not r.get("posts"):
            continue
        community = community_by_id.get(r["community_id"], {})
        ctype = community.get("type")
        params = community.get("params", {})
        try:
            if ctype == "reddit":
                _fetch_reddit_contents(r["posts"], params)
            elif ctype == "rss" and GEEKNEWS_DOMAIN in (params.get("feed_url") or ""):
                _fetch_geeknews_contents(r["posts"])
            elif ctype in ("rss", "hn_algolia"):
                _fetch_generic_link_contents(r["posts"])
            else:
                for p in r["posts"]:
                    p["content"] = None
        except Exception as e:  # noqa: BLE001 - 커뮤니티 단위 방어
            logger.warning("%s: 본문 확보 단계 실패(%s) — 전체 글 제목만으로 진행",
                           r.get("community_name"), e)
            for p in r["posts"]:
                p.setdefault("content", None)
