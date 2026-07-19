#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_pdf.py — 커뮤니티 다이제스트 PDF 문서 생성(fpdf2, 표준 라이브러리 예외 1건).

한글 렌더링:
  fonts/NotoSansKR-Regular.ttf, fonts/NotoSansKR-Bold.ttf를 사용한다.
  출처: Google Fonts GitHub(google/fonts 저장소, ofl/notosanskr)의 가변 폰트
  NotoSansKR[wght].ttf를 fonttools(varLib.instancer)로 wght=400/700 정적
  인스턴스로 변환했다(2026-07-19 — 해당 저장소 경로에 더 이상 별도 정적
  Regular/Bold TTF가 없고 가변 폰트 1개만 있음을 실측 확인). 상세 출처·변환
  절차는 fonts/SOURCE.txt, 라이선스는 fonts/OFL.txt(SIL OFL 1.1) 참고.

구성: 헤더(제목·수집 창 KST·총건수) → communities.json 순서의 커뮤니티별 섹션
(글마다 한글 제목/원제/지표/한글 요약/원문 링크) → 말미 [요약 실패]/[수집 실패] 목록.

출력: reports/digest-YYYY-MM-DD.pdf (window_end의 KST 날짜 기준).

전제: results의 각 post는 이미 fetch_content.py(content)·summarize.py
(translated_title/summary/summary_failed)를 거쳤고, 표시할 top_n만큼 호출측이
미리 슬라이스했다고 가정한다(이 모듈은 재슬라이스하지 않고 주어진 그대로 렌더링).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from collect import KST

logger = logging.getLogger("make_pdf")

REPORTS_DIR = "reports"
FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT_FAMILY = "NotoSansKR"
REGULAR_FONT_PATH = FONT_DIR / "NotoSansKR-Regular.ttf"
BOLD_FONT_PATH = FONT_DIR / "NotoSansKR-Bold.ttf"

PAGE_FORMAT = "A4"
MARGIN_MM = 15

TITLE_SIZE = 18
SUBTITLE_SIZE = 11
SECTION_SIZE = 13
FAIL_HEADING_SIZE = 12
POST_TITLE_SIZE = 12
META_SIZE = 9
BODY_SIZE = 10.5
LINK_SIZE = 9.5
FAIL_BODY_SIZE = 10

COLOR_TEXT = (30, 30, 30)
COLOR_MUTED = (110, 110, 110)
COLOR_LINK = (30, 80, 190)
COLOR_RULE = (200, 200, 200)
COLOR_SECTION_BG = (240, 242, 245)

ERROR_DISPLAY_LIMIT = 160  # 수집 실패 사유를 PDF에 그대로 길게 싣지 않기 위한 제한


def _one_line(msg: str, limit: int = ERROR_DISPLAY_LIMIT) -> str:
    flat = " ".join(str(msg).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _fmt_kst(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def _fmt_created(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        return datetime.fromisoformat(iso_str).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso_str


class DigestPDF(FPDF):
    """다이제스트 전용 페이지 조립 헬퍼(fpdf2.FPDF 상속)."""

    def __init__(self) -> None:
        super().__init__(format=PAGE_FORMAT, unit="mm")
        if not REGULAR_FONT_PATH.exists() or not BOLD_FONT_PATH.exists():
            raise RuntimeError(
                f"한글 폰트 파일이 없습니다: {REGULAR_FONT_PATH}, {BOLD_FONT_PATH} "
                f"(fonts/SOURCE.txt 참고해 재생성하세요)"
            )
        self.set_margins(MARGIN_MM, MARGIN_MM, MARGIN_MM)
        self.set_auto_page_break(auto=True, margin=MARGIN_MM)
        self.add_font(FONT_FAMILY, "", str(REGULAR_FONT_PATH))
        self.add_font(FONT_FAMILY, "B", str(BOLD_FONT_PATH))
        self.set_font(FONT_FAMILY, "", BODY_SIZE)

    def _rule(self) -> None:
        self.set_draw_color(*COLOR_RULE)
        y = self.get_y()
        self.line(self.l_margin, y, self.w - self.r_margin, y)

    def header_block(self, window_start: datetime, window_end: datetime,
                     total_posts: int, total_summarized: int) -> None:
        self.set_font(FONT_FAMILY, "B", TITLE_SIZE)
        self.set_text_color(*COLOR_TEXT)
        self.cell(0, 10, text="커뮤니티 인기 글 다이제스트", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        self.set_font(FONT_FAMILY, "", SUBTITLE_SIZE)
        self.set_text_color(*COLOR_MUTED)
        window_text = f"수집 창: {_fmt_kst(window_start)} ~ {_fmt_kst(window_end)} (KST)"
        self.cell(0, 7, text=window_text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.cell(0, 7, text=f"총 {total_posts}건 · 요약 {total_summarized}건",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(2)
        self._rule()
        self.ln(4)
        self.set_text_color(*COLOR_TEXT)

    def section_heading(self, name: str) -> None:
        self.set_font(FONT_FAMILY, "B", SECTION_SIZE)
        self.set_text_color(*COLOR_TEXT)
        self.set_fill_color(*COLOR_SECTION_BG)
        self.cell(0, 9, text=f"  {name}", new_x=XPos.LMARGIN, new_y=YPos.NEXT, fill=True)
        self.ln(2)

    def empty_notice(self, text: str) -> None:
        self.set_font(FONT_FAMILY, "", BODY_SIZE)
        self.set_text_color(*COLOR_MUTED)
        self.cell(0, 7, text=f"  {text}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(2)
        self.set_text_color(*COLOR_TEXT)

    def post_block(self, index: int, post: dict) -> None:
        display_title = post.get("translated_title") or post["title"]

        self.set_font(FONT_FAMILY, "B", POST_TITLE_SIZE)
        self.set_text_color(*COLOR_TEXT)
        self.multi_cell(0, 7, text=f"{index}. {display_title}",
                        new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        if post.get("translated_title") and post["translated_title"] != post["title"]:
            self.set_font(FONT_FAMILY, "", META_SIZE)
            self.set_text_color(*COLOR_MUTED)
            self.multi_cell(0, 5.5, text=f"원제: {post['title']}",
                            new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        meta_bits = [post.get("score_label") or "-"]
        created = _fmt_created(post.get("created_iso") or "")
        if created:
            meta_bits.append(created)
        self.set_font(FONT_FAMILY, "", META_SIZE)
        self.set_text_color(*COLOR_MUTED)
        self.cell(0, 5.5, text=" · ".join(meta_bits), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

        self.set_font(FONT_FAMILY, "", BODY_SIZE)
        summary = post.get("summary")
        if summary:
            self.set_text_color(*COLOR_TEXT)
            self.multi_cell(0, 6, text=summary, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        else:
            self.set_text_color(*COLOR_MUTED)
            self.multi_cell(0, 6, text="요약 실패 — 원문 링크를 참고하세요.",
                            new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        self.set_font(FONT_FAMILY, "", LINK_SIZE)
        self.set_text_color(*COLOR_LINK)
        self.multi_cell(0, 5.5, text=post["url"], new_x=XPos.LMARGIN, new_y=YPos.NEXT,
                        link=post["url"])

        self.set_text_color(*COLOR_TEXT)
        self.ln(3)

    def failure_section(self, heading: str, lines: list) -> None:
        if not lines:
            return
        self.ln(2)
        self._rule()
        self.ln(3)
        self.set_font(FONT_FAMILY, "B", FAIL_HEADING_SIZE)
        self.set_text_color(*COLOR_TEXT)
        self.cell(0, 8, text=heading, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_font(FONT_FAMILY, "", FAIL_BODY_SIZE)
        self.set_text_color(*COLOR_MUTED)
        for line in lines:
            self.multi_cell(0, 6, text=f"- {line}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(*COLOR_TEXT)


def build_pdf(results: list, communities: list,
             window_start: datetime, window_end: datetime,
             output_dir: str = REPORTS_DIR) -> Path:
    """다이제스트 PDF를 생성해 reports/digest-YYYY-MM-DD.pdf로 저장하고 경로를 반환한다."""
    community_order = [c.get("id") for c in communities]
    by_id = {r["community_id"]: r for r in results}

    ok_results = [r for r in results if not r.get("error")]
    fail_results = [r for r in results if r.get("error")]

    total_posts = sum(len(r["posts"]) for r in ok_results)
    total_summarized = sum(
        1 for r in ok_results for p in r["posts"] if not p.get("summary_failed")
    )

    pdf = DigestPDF()
    pdf.add_page()
    pdf.header_block(window_start, window_end, total_posts, total_summarized)

    # communities.json 순서를 그대로 따른다. enabled=false로 수집 단계에서 스킵된
    # 커뮤니티는 results 자체에 없으므로(collect.collect_all 참고) 자연히 제외된다.
    for cid in community_order:
        r = by_id.get(cid)
        if r is None or r.get("error"):
            continue
        pdf.section_heading(r["community_name"])
        if not r["posts"]:
            pdf.empty_notice("72시간 내 수집된 글 없음")
            continue
        for i, p in enumerate(r["posts"], start=1):
            pdf.post_block(i, p)

    summary_failed_lines = [
        f"{r['community_name']} - {p['title']} ({p['url']})"
        for r in ok_results for p in r["posts"] if p.get("summary_failed")
    ]
    pdf.failure_section("[요약 실패]", summary_failed_lines)

    collect_failed_lines = [
        f"{r['community_name']}: {_one_line(r['error'])}" for r in fail_results
    ]
    pdf.failure_section("[수집 실패]", collect_failed_lines)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    date_str = window_end.astimezone(KST).strftime("%Y-%m-%d")
    out_path = out_dir / f"digest-{date_str}.pdf"
    pdf.output(str(out_path))
    logger.info("PDF 생성 완료: %s (총 %d건, 요약 %d건)", out_path, total_posts, total_summarized)
    return out_path
