# 새 PC 마이그레이션 가이드

작성: 2026-07-20. 대상: 이 저장소(`StockHedge/vibecoding-digest`)와 그 운영 환경을 새 Windows PC로 이관.

## 0. 핵심 사실 — 서두르지 않아도 된다

**다이제스트 루틴 자체는 GitHub Actions에서 돌고 있어 새 PC 설정과 무관하게 계속 동작한다**
(3일마다 09:00 KST 기준, cron 지터로 실제 발송은 최대 +3~4시간 지연 가능 — 2026-07-20 실측 3.5h).
새 PC 이관은 "유지보수 능력"의 이관이지 "루틴 생존"의 이관이 아니다.

## 1. 시스템 개요

```
[GitHub Actions cron 매일 00:00 UTC] → epoch-day%3 게이트(기준일 2026-07-21)
  → collect.py  (7개 커뮤니티: r/ClaudeCode·r/ClaudeAI·r/vibecoding·HN×2·Lobsters·긱뉴스)
  → fetch_content.py (글 본문 확보)
  → summarize.py (Gemini 한글 번역·요약, 기본 gemini-3.5-flash)
  → make_pdf.py  (fpdf2 + fonts/NotoSansKR)
  → publish.py   (reports/ 커밋·푸시, blob/<현재 브랜치> 링크 생성)
  → kakao_sender.py (나에게 보내기 — 헤더+하이라이트+PDF 링크 2조각)
```

- 커뮤니티 추가/제거 = `communities.json` 수정 → 커밋·푸시가 전부.
- 근거 리서치: 구 PC `~/Documents/research/vibe-coding-communities/` (원문 101건 custody — 필요 시 복사).

## 2. 새 PC 필수 절차

1. 도구: git ≥2.40, GitHub CLI(`gh`), Python 3.12+(`py` 런처). `gh auth login` (계정 **StockHedge**).
2. 클론: `gh repo clone StockHedge/vibecoding-digest` → 폴더 진입.
3. 셋업 스크립트: `powershell -ExecutionPolicy Bypass -File setup_new_pc.ps1`
   (도구 점검 / pip 의존성 / gitleaks 설치·pre-commit 훅 / .env 골격 생성까지 자동).
4. 시크릿 복원: 3절 표에 따라 `.env` 4개 키를 채운다.
5. 검증: `py -3 kakao_sender.py --check` → 전부 set 확인 → `py -3 main.py --dry-run`
   (전송·푸시 없이 수집→요약→PDF까지 완주 확인).

## 3. 시크릿 표 (값은 절대 이 문서·저장소에 넣지 말 것)

| 키 | 용도 | 복원 방법 |
| --- | --- | --- |
| `KAKAO_REST_API_KEY` | 카카오 앱 식별 | 구 PC `.env`에서 복사, 또는 developers.kakao.com > 내 애플리케이션 > **vibecodingsendmyself**(ID 1517043) > 앱 키 |
| `KAKAO_CLIENT_SECRET` | 토큰 교환 인증 | 위 콘솔 > 보안 (활성화 상태) |
| `KAKAO_REFRESH_TOKEN` | 전송 토큰 갱신 | 구 PC `.env` 복사, 또는 새 PC에서 `py -3 kakao_auth.py` 재발급 (콘솔 설정은 유지돼 있어 브라우저 동의만 하면 됨) |
| `GEMINI_API_KEY` | 요약·번역 | 구 PC `.env` 복사, 또는 aistudio.google.com에서 재발급 |

- 이관 경로는 오프라인 수단(USB·비밀번호 관리자) 권장. 채팅·메일 평문 전송 지양.
- **GitHub Secrets(클라우드용)는 이미 등록돼 있어 손댈 필요 없다** (KAKAO 3종 + GEMINI_API_KEY).
- 미등록 선택 항목: `REDDIT_CLIENT_ID/SECRET`(Reddit Responsible Builder 승인 후), `ADMIN_TOKEN`(PAT — 토큰 자동 회전용).

## 4. Claude Code 자산 이관 (구 PC → 새 PC, 선택이지만 권장)

| 자산 | 구 PC 경로 | 비고 |
| --- | --- | --- |
| 전역 지침 | `~/.claude/CLAUDE.md` | 모델 정책·스킬 라우팅 전부 |
| 스킬 10종 | `~/.claude/skills/` | deepresearch는 2026-07-18 함정 3건 반영본 |
| deepresearch 스크립트 | `~/.claude/scripts/fetch_raw.py` 외 3종 | guard.py·runner_stop_gate.py 포함 필수 |
| 러너/워크플로 | `~/.claude/agents/research-runner.md`, `~/.claude/workflows/research-verify.js` | |
| 설정 | `~/.claude/settings.json` | permissions.allow의 `py -3 ~/…research_gate.py` literal prefix 유지 |
| MCP | Tavily 서버 설정(API 키 포함) | 키는 시크릿 취급 |
| 프로젝트 메모리 | `~/.claude/projects/<경로 슬러그>/memory/` | 슬러그가 **프로젝트 절대경로 기반**이라 새 PC에서 경로가 다르면 폴더명이 달라짐 — 새 세션 프롬프트가 메모리를 재생성하므로 복사 생략 가능 |

일괄 복사 예 (구 PC PowerShell):
`Compress-Archive -Path "$env:USERPROFILE\.claude\CLAUDE.md","$env:USERPROFILE\.claude\skills","$env:USERPROFILE\.claude\scripts","$env:USERPROFILE\.claude\agents","$env:USERPROFILE\.claude\workflows" -DestinationPath claude-assets.zip`

## 5. 운영 런북 (새 PC의 Claude 메모리로 옮길 내용)

- **정상 상태**: Actions가 3일마다 발송. 확인: `gh run list --repo StockHedge/vibecoding-digest`.
- **카톡이 안 옴**: ① run 실패 여부 확인 ② 로그에서 `invalid_grant` → 로컬 `py -3 kakao_auth.py`
  재인증 후 `gh secret set KAKAO_REFRESH_TOKEN` 재등록.
- **토큰 회전 경고 카톡** ("Secret 수동 갱신 필요"): 회전된 토큰이 Secret에 반영 안 된 상태 —
  로컬 재인증 → Secret 재등록 (ADMIN_TOKEN PAT 등록 시 자동화됨).
- **Reddit 제약**: `.json` 403 고정, `.rss` 폴백으로 수집(점수 없음). 로컬·클라우드 공통.
  레이트리밋 헤더 기반 대기 로직이 collect.py에 있음.
- **긱뉴스**: 피드는 `news.hada.io/rss/news`만 정상 (`/rss`는 403).
- **로컬 스케줄러 금지**: 2026-07-19 제거됨. 재등록하면 중복 발송된다.
- **PDF 링크**: 비공개 repo라 열람 기기에서 GitHub 로그인 필요.
- 실전송 수동 실행: `py -3 main.py` / 텍스트 구방식: `--legacy-text` / 시험: `--dry-run`.

## 6. 구 PC 정리 (이관 완료 확인 후)

- `.env` 파기(또는 보관 정책에 따름). 로컬 스케줄 작업은 이미 없음.
- `~/Documents/research/` 리서치 원문은 필요 시 백업 후 정리.

## 7. 새 세션 부트스트랩 프롬프트

새 PC에서 클론한 저장소 폴더에서 Claude Code를 열고 아래를 붙여넣는다:

```text
이 저장소는 이전 PC에서 구축 완료된 "바이브 코딩 커뮤니티 다이제스트" 시스템이다.
루틴은 GitHub Actions(StockHedge/vibecoding-digest)에서 이미 3일 주기로 돌고 있으므로
네 임무는 루틴 재구축이 아니라 "이 PC를 유지보수 거점으로 복원"하는 것이다.

먼저 MIGRATION.md와 README.md를 읽어라. 그다음 순서대로:
1. 환경 점검·셋업: setup_new_pc.ps1 실행 (도구·의존성·gitleaks 훅·.env 골격).
2. 시크릿 복원: MIGRATION.md 3절 표대로 .env 4개 키를 채운다. 값이 필요한 시점에
   내게 물어라 — 내가 직접 넣거나 값을 제공한다. KAKAO_REFRESH_TOKEN이 없으면
   py -3 kakao_auth.py 재인증으로 발급하라 (카카오 콘솔 설정은 유지돼 있다).
3. 검증: py -3 kakao_sender.py --check 전 키 set 확인 → py -3 main.py --dry-run 완주
   확인 (전송·푸시 없음). 실전송 테스트는 내 확인을 받은 뒤에만.
4. MIGRATION.md 5절 "운영 런북"을 이 프로젝트의 Claude 메모리(project 타입)로 저장하라.
5. (전역 자산을 복사해 뒀다면) deepresearch 사전점검 6종 파일 존재를 확인해 보고하라.

제약: 시크릿을 stdout/로그에 노출하지 말 것(.env 전체 출력 금지, 마스킹 진단만).
.env 커밋 금지(gitleaks 훅 필수). 로컬 작업 스케줄러를 등록하지 말 것(클라우드와
중복 발송된다). 완료 후 상태를 요약 보고하라.
```
