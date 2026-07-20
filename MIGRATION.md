# 새 PC 마이그레이션 가이드 (상세판)

작성: 2026-07-20. 대상: `StockHedge/vibecoding-digest` 저장소와 그 운영 환경의 Windows PC 간 이관.
총 소요 약 1시간 (재발급 경로 선택 시 +15분).

## 0. 핵심 사실 — 서두르지 않아도 된다

**다이제스트 루틴은 GitHub Actions에서 돌고 있어 새 PC 설정과 무관하게 계속 동작한다**
(3일 주기 09:00 KST 기준, cron 지터로 실제 발송은 최대 +3~4시간 지연 — 2026-07-20 실측 3.5h).
이관은 "유지보수 거점"의 이관이지 "루틴 생존"의 이관이 아니다. 며칠 걸려도 발송은 계속된다.

## 1. 자산 지도 — 무엇이 어디에 있나

| 위치 | 자산 | 이관 |
| --- | --- | --- |
| 클라우드 | repo(코드·reports/·MIGRATION.md), Actions 워크플로, GitHub Secrets 4종 | **불필요** — 그대로 동작 |
| 구 PC만 | `.env`(시크릿 4종) | **필수** — 3단계 중 택1로 이관 |
| 구 PC만 | `~/.claude` 자산 번들(claude-assets.zip, 바탕 화면, 89KB) | 권장 |
| 구 PC만 | Tavily MCP API 키 (Claude Code MCP 설정 내) | 딥리서치 쓰면 필수 |
| 구 PC만 | `~/Documents/research/` 리서치 원문 custody | 선택 (백업) |
| 새 PC 생성 | 도구 설치, gh 로그인, gitleaks 훅, (선택) 카카오 재인증 | 아래 절차 |

## 2. Phase 1 — 구 PC에서 챙길 것 (10분)

1. **`.env` 이관 방법 3택1** (파일: `C:\Users\<사용자>\project\vibecoding\.env`):
   - **(a) 비밀번호 관리자 (권장)**: 4개 키를 항목으로 저장 → 새 PC에서 꺼내 입력.
   - **(b) USB 파일 복사**: 사용 후 USB에서 삭제.
   - **(c) 전부 재발급 (가장 깨끗, +15분)**: 아무것도 안 옮기고 새 PC에서 —
     REST 키·Client Secret은 카카오 콘솔에서 복사, refresh token은 `kakao_auth.py` 재인증,
     Gemini는 AI Studio 재발급. (4절 표 참조)
   - 어떤 경우든 **채팅·메일 평문 전송 지양**.
2. **claude-assets.zip** (바탕 화면) → USB. 내용: 전역 CLAUDE.md, 스킬 10종, deepresearch
   스크립트 4종, research-runner, research-verify.js. 시크릿 없음.
3. (선택) **Tavily API 키** — 구 PC에서 `claude mcp list`로 서버 확인, 키 값을 비밀번호
   관리자에 보관 (설정 파일 통째 복사 대신 키만).
4. (선택) `~/Documents/research/` 백업 (vibe-coding-communities: raw 101건 + research.md).
5. 구 PC에 남은 정리 대상은 7절 참조 (로컬 스케줄러는 이미 제거된 상태).

## 3. Phase 2 — 새 PC 기본 도구 (20~30분)

관리자 PowerShell에서:

```powershell
winget install Git.Git
winget install GitHub.cli
winget install Python.Python.3.12   # py 런처 포함. Microsoft Store 파이썬 비권장
```

- 확인: `git --version` / `gh --version` / `py -3 --version` (3.12+).
- `gh auth login` → GitHub.com → HTTPS → 브라우저 로그인 (**StockHedge** 계정).
  이 과정에서 git 자격증명도 연결된다 (`gh auth status`로 확인).
- **Claude Code 설치** + 로그인 (기존 구독 계정).
- 함정 예방: 새 PC **Windows 사용자명은 ASCII 권장** — 한글 경로는 Node 계열 도구에서
  인코딩 오류를 유발한 전력이 있다.

## 4. Phase 3 — 저장소 복원 (5분)

```powershell
mkdir "$env:USERPROFILE\project" -Force
gh repo clone StockHedge/vibecoding-digest "$env:USERPROFILE\project\vibecoding"
cd "$env:USERPROFILE\project\vibecoding"
powershell -ExecutionPolicy Bypass -File setup_new_pc.ps1
```

setup 스크립트가 하는 일: 도구 점검 → `pip install -r requirements.txt`(fpdf2) →
gitleaks 설치(winget)·이식형 pre-commit 훅 작성 → `.env` 골격 생성(.env.example 복사) →
마스킹 키 진단. 각 단계 `[ OK ]` 출력을 확인하라.

## 5. Phase 4 — 시크릿 복원 (5~15분)

`.env`의 4개 키 (값은 절대 이 문서·저장소·로그에 넣지 말 것):

| 키 | 복원 방법 |
| --- | --- |
| `KAKAO_REST_API_KEY` | (a/b) 붙여넣기, 또는 developers.kakao.com > 내 애플리케이션 > **vibecodingsendmyself**(ID 1517043) > 앱 설정 > 앱 키 |
| `KAKAO_CLIENT_SECRET` | (a/b) 붙여넣기, 또는 위 콘솔 > 앱 설정 > 보안 |
| `KAKAO_REFRESH_TOKEN` | (a/b) 붙여넣기, 또는 `py -3 kakao_auth.py` — 브라우저 동의만 하면 자동 저장 (콘솔의 Redirect URI `http://localhost:8888/callback`·talk_message 설정은 유지돼 있음) |
| `GEMINI_API_KEY` | (a/b) 붙여넣기, 또는 aistudio.google.com에서 재발급 |

- refresh token을 **재발급해도 클라우드(GitHub Secrets)의 기존 토큰과 공존**한다 —
  Actions 발송은 영향 없음. 클라우드 토큰도 교체하고 싶을 때만:
  `grep '^KAKAO_REFRESH_TOKEN=' .env | cut -d= -f2- | gh secret set KAKAO_REFRESH_TOKEN --repo StockHedge/vibecoding-digest`
- 검증: `py -3 kakao_sender.py --check` → 4키 모두 `set (N chars)`.
- GitHub Secrets는 이미 등록돼 있어 **손대지 않는다**. 미등록 선택 항목:
  `REDDIT_CLIENT_ID/SECRET`(Reddit Responsible Builder 승인 후), `ADMIN_TOKEN`(PAT, 토큰 자동 회전용).

## 6. Phase 5 — Claude Code 환경 복원 (10분)

1. claude-assets.zip을 `%USERPROFILE%\.claude\`에 풀기 →
   `CLAUDE.md`, `skills\`, `scripts\`, `agents\`, `workflows\` 배치 확인.
2. `settings.json`은 번들에 없다(기기 종속·시크릿 포함 가능) — 새 PC에서 권한 프롬프트에
   응답하며 자연 재구성하거나 `fewer-permission-prompts` 스킬로 재구축.
   deepresearch용 허용 규칙(`py -3 ~/.claude/scripts/research_gate.py` literal prefix)은
   첫 실행 때 다시 허용하면 된다.
3. Tavily MCP 재연결: `claude mcp add`(보관해 둔 키 사용).
4. 프로젝트 메모리는 복사하지 않는다 — 메모리 폴더명이 **프로젝트 절대경로 기반**이라
   새 PC에서 달라진다. 8절 부트스트랩 프롬프트가 런북(7절)을 메모리로 재생성한다.

## 7. Phase 6 — 첫 세션 검증 + 운영 런북

클론 폴더에서 `claude` 실행 → 9절 프롬프트 붙여넣기. Claude가 수행:
셋업 재확인 → `--check` → `py -3 main.py --dry-run`(6~8분, 전송·푸시 없음) →
런북 메모리 저장 → deepresearch 사전점검 6종 보고.

사람 체크포인트: dry-run 로그 "수집 완료: 성공 7 / 실패 0" + `reports/` PDF 열어 한글 확인.
실전송 검증까지 원하면 `py -3 main.py` 1회 (카톡 2조각 + PDF 링크 도착).

**운영 런북** (메모리로 저장될 내용):
- 정상 상태 확인: `gh run list --repo StockHedge/vibecoding-digest`
- 카톡 미수신: run 실패 확인 → 로그 `invalid_grant`면 로컬 `py -3 kakao_auth.py` 재인증
  후 `gh secret set KAKAO_REFRESH_TOKEN` 재등록
- "Secret 수동 갱신 필요" 경고 카톡: 토큰 회전이 Secret에 미반영 — 위와 동일 절차
- Reddit: `.json` 403 고정 → `.rss` 폴백 수집(점수 없음), 레이트리밋 헤더 대기 로직 내장
- 긱뉴스 피드는 `news.hada.io/rss/news`만 정상 (`/rss` 403)
- **로컬 스케줄러 등록 금지** (클라우드와 중복 발송)
- PDF 링크는 비공개 repo → 열람 기기에서 GitHub 로그인 필요
- 커뮤니티 변경 = `communities.json` 수정 → 커밋·푸시

## 8. Phase 7 — 구 PC 정리 (이관 검증 후)

- `.env` 파기, 바탕 화면 claude-assets.zip 삭제, USB 내 사본 삭제.
- (선택) `gh auth logout`, 리서치 원문 백업 확인 후 정리.
- 로컬 스케줄 작업은 2026-07-19 이미 제거됨 — 추가 조치 없음.

## 9. 새 세션 부트스트랩 프롬프트

새 PC 클론 폴더에서 Claude Code를 열고 아래를 붙여넣는다:

```text
이 저장소는 이전 PC에서 구축 완료된 "바이브 코딩 커뮤니티 다이제스트" 시스템이다.
루틴은 GitHub Actions(StockHedge/vibecoding-digest)에서 이미 3일 주기로 돌고 있으므로
네 임무는 루틴 재구축이 아니라 "이 PC를 유지보수 거점으로 복원"하는 것이다.

먼저 MIGRATION.md와 README.md를 읽어라. 그다음 순서대로:
1. 환경 점검·셋업: setup_new_pc.ps1 실행 (도구·의존성·gitleaks 훅·.env 골격).
2. 시크릿 복원: MIGRATION.md 5절 표대로 .env 4개 키를 채운다. 값이 필요한 시점에
   내게 물어라 — 내가 직접 넣거나 값을 제공한다. KAKAO_REFRESH_TOKEN이 없으면
   py -3 kakao_auth.py 재인증으로 발급하라 (카카오 콘솔 설정은 유지돼 있다).
3. 검증: py -3 kakao_sender.py --check 전 키 set 확인 → py -3 main.py --dry-run 완주
   확인 (전송·푸시 없음). 실전송 테스트는 내 확인을 받은 뒤에만.
4. MIGRATION.md 7절 "운영 런북"을 이 프로젝트의 Claude 메모리(project 타입)로 저장하라.
5. (전역 자산을 복사해 뒀다면) deepresearch 사전점검 6종 파일 존재를 확인해 보고하라.

제약: 시크릿을 stdout/로그에 노출하지 말 것(.env 전체 출력 금지, 마스킹 진단만).
.env 커밋 금지(gitleaks 훅 필수). 로컬 작업 스케줄러를 등록하지 말 것(클라우드와
중복 발송된다). 완료 후 상태를 요약 보고하라.
```
