# 강지호 (Jiho Kang) — Global Claude Code Preferences

<!-- 최종 갱신: 2026-07-05 v2.3
     v2:   1차 리뷰 수용 7건 (승격 전 컨텍스트 리셋, auto-compact 캐시 인지, claude-api fallback 스펙,
           pwsh 7 표준화, temp 파일명 고유화, env 주입 override, gitleaks 훅, 단계적 종료)
     v2.1: 2차 리뷰 수용 3건 (변동성 수치 기준일 병기·갱신 의무 확장, 수동 fallback 스택 예외,
           강행 규정 충돌 시 판단 원칙)
     v2.2: 인시던트 런북 5종을 사용자 스킬로 분리 (상시 토큰 절감)
     v2.3: Boris Cherny 워크플로우 대조 리뷰 반영 8건 —
           1) Verification Loop 신설: 검증을 opt-in → 기본값으로 전환 (verify/run 라우팅은 수단으로 존치)
           2) 명세 게이트(Plan 선행) 복원 — v2.2에서 누락되었던 항목
           3) Sonnet 5 (2026-07-01) 반영: 별칭 타깃 검증 의무 신설, escalation 재조정, max_tokens 여유
           4) effort 체계 xhigh 인지 (Fable 지원 여부 미검증 — 확인 후 갱신)
           5) claude-api Fallback 스펙 상세 → claude-api 스킬 본문으로 이관 (트리거만 잔류)
           6) Python override 패턴 → 사용자 스킬 py-oneoff-override로 이관 (원칙 1줄 잔류)
           7) Code Quality를 판단 규칙만 남기고 압축 — 스타일 강제는 훅/린터(결정적 계층)로 이양
           8) Fable 운영 유의사항을 세션 판단에 필요한 항목 위주로 압축 -->

## Model Policy (2026-07 갱신)

### 별칭 우선 원칙
- CLAUDE.md 및 에이전트 frontmatter에는 전체 모델 ID 대신 별칭을 사용한다:
  `fable` / `opus` / `sonnet` / `haiku`.
- 전체 ID 핀(`claude-fable-5`, `claude-opus-4-8`, `claude-sonnet-5`(추정·미검증),
  `claude-haiku-4-5`)은 재현성이 필요한 API 호출 코드에서만 사용한다.
- 모델 세대 교체 시 두 가지를 즉시 수행한다 (구버전 ID 잔류 금지 — `opus-4-7` 잔류 사례):
  1. 이 섹션의 ID 표기와 변동성 수치(단가, fallback 허용 대상, 베타 상태) 갱신.
  2. **별칭 타깃 재검증** — `/model`로 각 별칭이 실제 가리키는 모델을 확인한다.
     별칭 의미가 조용히 바뀌면 escalation 계층 전체의 전제가 바뀐다
     (2026-07 Sonnet 5 출시 사례에서 학습).

### 라우팅 기준: 중요도가 아닌 태스크 구조
- 판단 단위는 **완료된 태스크당 비용**(cost per finished task)이다. 토큰당 단가가 아니다.
  재조종(re-steering) 턴 비용을 포함해 계산한다 — 하위 모델이 항상 싸지 않다.
- Fable의 프리미엄(Opus 4.8의 2배)은 첫 시도 정확도와 자가 검증이 재작업 턴 수를
  줄여주는 **장기 멀티스테이지 작업**에서만 회수된다.
- Sonnet이 한 번에 끝낼 수 있는 작업에 Fable을 쓰면 thoroughness는 순수 오버헤드다.

### Escalation 정책 (하위 시작 → 실패 시 승격)
1. `haiku` — 탐색·grep·위치 찾기. 빌트인 Explore의 Haiku 기본값은 변경하지 않는다.
2. `sonnet` — **기본값.** 파일 수정, 반복 실행, API 호출 자동화, 문서 작성, 코드 리뷰 등
   정형 태스크 전부.
   - Sonnet 5 (2026-07-01 출시) 유의: high~max effort에서 일부 작업이 Opus 4.8에 근접.
     단 Sonnet 4.6 대비 토큰 소비 약 +30% — **API 호출 코드에서 max_tokens 여유 필수**
     (부족 시 thinking이 예산을 소진해 답변이 잘린다). 기존 파이프라인(Stage 2·3)의
     max_tokens·단가 재산정 대상.
3. `opus` (Opus 4.8) — 다음 조건에서 승격
   (Sonnet 5 성능 향상으로 승격 빈도 축소 예상 — 실측 후 기준 재조정, 2026-07 기준 잠정):
   - 수백 파일 이상 규모의 코드베이스 전체 분석
   - 복잡한 버그의 멀티-레이어 근본 원인 추적 —
     단, Sonnet 5 + effort 상향으로 1회 시도 후 실패 시 승격
   - 아키텍처 수준의 설계 결정 (트레이드오프 분석 포함)
   - 사이버보안 인접 작업(security-review 대상: 인증·token·secret·SQL·OAuth) —
     Fable 안전 분류기의 fallback을 기다리지 말고 처음부터 Opus 직행 (기준 유지)
4. `fable` — 아래 3가지 경우로 **제한**:
   - 장기 멀티스테이지 자율 세션 (계획 → 위임 → 자가검증 사이클이 수 시간 이상 지속)
   - Opus에서 실패·교착된 작업의 승격 — 단, **승격 전 컨텍스트 초기화 + 접근 재구성
     (프롬프트 재작성) 1회 필수**. 교착의 상당수는 능력 한계가 아니라 실패 접근법으로
     오염된 컨텍스트가 원인이며, 이 경우 모델을 승격해도 같은 실패를 반복한다.
     리셋 후에도 실패할 때만 Fable로 승격.
   - 영향 범위(blast radius)가 큰 아키텍처 결정

- 예외: 과거 실패 이력이 있거나 난이도가 확실한 작업은 처음부터 Fable로 시작한다.
  (Sonnet 실패 1회 + Fable 재시도 비용 > 처음부터 Fable 비용)
- 서브에이전트에서 `fable`은 원칙적 비사용. Fable의 강점(장기 계획·위임·자가검증)은
  메인 thread 역할이며, 단발 분담 태스크에는 비용 대비 효익이 낮다.
  예외: 메인이 Sonnet인 세션에서 단일 초고난도 추론 태스크를 1회 위임할 때.

### Fable 5 운영 유의사항 (세션 판단용 — API 통합 상세는 claude-api 스킬)
- 단가: $10 / $50 per MTok, Opus 4.8의 2배 (2026-07 기준 — 변동성 수치, 갱신 대상).
  프롬프트 캐싱 90% 입력 할인 — 장기 세션일수록 캐시 적중률이 실비용을 좌우한다.
- **캐시는 prefix 일치 기반**: 컨텍스트 후미 추가는 캐시 친화적, 캐시를 깨는 것은
  prefix 변경(auto-compact의 히스토리 재작성, 시스템 프롬프트·도구 정의 변경).
  장기 세션에서는 한도 임박 전 **작업 페이즈 경계에서 수동 `/compact`**를 실행한다.
- **thinking 비활성화 불가**, 접힌 thinking도 전부 출력 단가($50)로 과금.
  장황한 출력 = Fable 비용 구조의 핵심.
- **effort가 1차 비용 제어 수단**: 기본값 high 유지. medium/low 하향은
  "Fable급 판단력은 필요하되 출력량 통제가 필요한 세션"에 한정.
  정형 작업이라면 effort 낮춘 Fable보다 Sonnet이 단가상 우위 — 모델 자체를 내린다.
  * xhigh effort (Opus 4.7부터 도입, 코딩·에이전틱 권장): Fable 지원 여부
    **미검증** — 확인 후 이 항목 갱신 (변동성).
- **간결성 지시 역설 금지**: Fable 작업에 "최종 답만 출력" 류의 강한 압축 지시를 걸면
  자가 검증(=Fable 단가를 지불하는 이유)을 억제하면서 비용은 그대로 낸다.
  출력 통제는 모델 라우팅과 effort로 하고, Fable 작업에서는 thoroughness를 허용.
- **구조적 완화가 최우선**: Fable 메인 thread의 출력은 계획·위임 지시·검증 판단
  (짧은 출력)에 국한하고, 대량 출력(코드 diff, 문서 본문)은 Sonnet 서브에이전트가
  생성하게 한다. API 파이프라인에서는 Advisor 도구(베타)가 이 패턴의 제품화 —
  상세는 claude-api 스킬.
- 안전 분류기: 사이버보안·생물학 쿼리는 Opus 4.8 응답으로 자동 대체될 수 있다
  (대체분은 Fable 단가 미청구). 디버깅 시 응답 모델이 달라질 수 있음을 인지.
- **reasoning_extraction 함정**: 내부 추론을 echo·전사하게 하는 지시가
  스킬·프롬프트·하네스에 있으면 거부 → Opus fallback 빈도가 높아진다.
  Fable 마이그레이션 시 기존 자산에서 해당 지시를 감사·제거.
- 메인 thread 모델은 CLAUDE.md로 강제되지 않는다 — 세션 시작 시 `/model` 또는
  settings에서 본 정책에 따라 직접 선택한다.

## Verification Loop (기본값) <!-- v2.3 신설 -->
- **기능적 변경(로직·API·UI·설정)을 완료하면 검증을 기본 수행한다.**
  검증 없이 완료 보고하는 경우, 왜 검증을 생략했는지 사유를 명시한다.
- 검증의 정의: 프로젝트의 테스트/타입체크/빌드 실행, 또는 실제 앱 구동 후 동작 관찰
  (`verify`/`run` 스킬은 이 루프의 수단이다 — 사용자 요청을 기다리지 않는다).
- **UI/프론트엔드 변경은 브라우저 실동작 확인까지가 검증 완료 기준**
  (스크린샷/DOM 확인 — Chrome 확장 또는 Playwright). 코드만 보고 완료 선언 금지.
- 프로젝트 CLAUDE.md에 검증 커맨드(test/typecheck/build)가 없으면:
  사용자에게 확인 후 해당 프로젝트 CLAUDE.md에 추가를 제안한다.
  검증 *수단*은 프로젝트 소관, 검증 *의무*는 전역 소관.
- 검증이 불가능한 환경이면 무엇을 검증하지 못했는지 완료 보고에 명시한다.

## Working Style
- 모든 대화와 코드 주석은 한국어 우선 (영문 라이브러리/식별자/표준 용어는 그대로 유지).
- 전문적이고 객관적인 톤. 감정이입·과장·이모티콘 배제.
- 표(table)는 비교가 명백히 필요한 경우에만 사용. 기본은 산문/리스트.
- 사용자의 의견을 무조건 긍정하지 말고, 사실 오류·논리 결함·간과된 리스크는 즉시 짚어줄 것.
- Senior engineer 수준의 코드 설계 기대. 매직 넘버, 미정리 import, 흩어진 책임 등 회피.
- 기존 MVP (Minimal Viable Product)의 개념과는 완전히 다른 MVP (Maximal Viable Product) 를 선호한다, 출시 전 가능한 최대한 많은 기능을 탑재해 출시하는 것을 선호한다.

## Code Quality <!-- v2.3: 스타일 강제는 훅/린터로 이양, 판단 규칙만 잔류 -->
- **스타일·포맷·타입 강제는 결정적 계층이 담당한다**: ruff / mypy(strict) /
  tsconfig strict / gitleaks + PostToolUse·pre-commit 훅. 린터 설정 파일이 진실의
  원천이며, CLAUDE.md에는 도구로 강제 불가능한 판단 규칙만 둔다.
- 에러 핸들링은 도메인 의미 기반 (단순 `try/except: pass` 금지).
- 외부 API 호출에는 timeout·retry·logging 기본 포함.
- 정기 작업의 일회성 파라미터 변경은 monkey-patch 금지 — env/CLI 주입 지점을 설계한다
  (상세: `py-oneoff-override` 스킬).

## Environment Constraints (Windows)
- 경로는 항상 `$env:USERPROFILE` (PowerShell) 또는 `Path.home()` (Python) 사용.
- 하드코딩 절대 금지 — 기기마다 사용자명이 다름. `Desktop` 표기 (`바탕 화면` 아닌).
- Node.js/일부 도구에서 한글 경로 인코딩 오류 발생 가능 — 그런 증상이면 우선 인코딩 원인 의심.

## Resource & Efficiency

### 원칙
- 작업 완성도와 효율이 토큰 비용보다 우선한다. 단, Fable 사용은 Model Policy의
  escalation 기준을 따른다 (품질 우선 ≠ 최상위 모델 남용).
- 메인 thread = **오케스트레이터+에디터** 자기 인식. 별도 "교통정리 에이전트" 불필요 —
  메인이 plan/track/verify 사이클을 직접 수행한다. Sub-agent는 *작업 분담* 전용,
  *조율* 자체는 메인이 담당.
- 스킬은 Skill Routing 섹션 기준으로 매칭되면 자동 적용한다.
- 반복 관찰되는 워크플로우(3회 이상 동일 절차)는 `.claude/commands/` 슬래시 명령어로
  자산화를 제안하고, 주기 실행이 필요하면 `loop`/`schedule`과 결합한다. <!-- v2.3 -->

### 병렬 실행 / Sub-agent 운영
- 태스크가 명확히 독립적일 때만 한 message에 Agent ×N 동시 호출 (`run_in_background: true`).
- 병렬화는 순차 처리보다 명확히 빠른 경우에만. 단일 태스크는 spawn 전에 메인 thread
  직접 처리 가능 여부를 먼저 판단한다.
- 파일 수정 계열 에이전트의 동시 실행 시 같은 파일 충돌 위험이 있으면
  frontmatter `isolation: worktree` 격리를 검토한다. <!-- v2.3 -->
- Sub-agent 보고 양식 강제: *변경 파일 N건 / 핵심 1~2문장 / 검증 결과 / 미해결 1줄*
  (100~150 단어).
- Sub-agent 결과는 *결정적 산물*(변경된 코드 / 명령 결과)만 채택. 요약은 검증용 참고.
- Sub-agent가 파일 수정 시 system이 자동으로 diff 표시 (turn당 5~15k 토큰) —
  같은 sub-agent에 **연속 작업 분담**해서 메인 thread의 file diff 노출 빈도를 절감한다.

### 토큰 누수 방지 패턴 (2026-05-27 학습 기반)
1. **Same-file batch edit**: 한 파일을 여러 번 수정해야 하면 *첫 Read 1회 + 모든 Edit를
   한 message에 multi-tool로 묶기*.
2. **Multi-locale parallel edit**: 같은 키를 여러 파일에 추가할 때는 한 message에
   Edit ×N 동시 호출. 순차 호출 금지.
3. **Bash output filtering at source**: 노이즈 많은 명령은 반드시 source에서 filter.
   `| grep -iE "error|trace" | tail -N` 패턴.

### 도구 라우팅 — 효율 우선
- 코드 검색: `Grep` only (Bash + grep 금지)
- 파일 검색: `Glob` only (Bash + find 금지)
- 파일 읽기: `Read` only (Bash + cat/head/tail 금지)
- 외부 모니터링: 가능하면 MCP 서버 활용 (Sentry MCP, GitHub MCP, Cloudflare MCP) → Bash 줄임

## Skill Routing

기본 원칙: **트리거 키워드가 매칭되면 사용자 명시 없이도 자동 적용**. 매칭이 모호하면 짧게 묻고 진행한다.

### 디자인 / UI
- `impeccable-foundation` — UI / CSS / React / Vue / Tailwind / 디자인 토큰 / 컴포넌트 작성·수정 시 무조건 적용. 백엔드·데이터·자동화 스크립트 작업에는 비적용.
- `bold-direction` — 외부 노출 페이지 (랜딩, 마케팅, IR 자료 웹, 포트폴리오, 1회성 시연용 아티팩트) 작성 시 `impeccable-foundation` 과 함께 적용.

### 검증 / 리뷰
<!-- v2.3: verify/run은 Verification Loop의 수단 — 명시 요청 없이도 루프 기준 충족 시 자동 발동 -->
- `verify` — 기능적 변경 완료 시 자동 (Verification Loop 참조), 또는 PR / 로컬 변경 확인 명시 요청 시. 실제 앱 실행 + 동작 관찰 단계 포함.
- `code-review` — diff 가 50줄 이상이거나 사용자가 리뷰 명시 요청 시. effort 기본 medium, 운영 코드·보안 관련은 high. `/code-review --fix`(발견 사항 working tree 적용)는 리뷰→수정 원스텝이 명확히 이득일 때만.
- `security-review` — 인증·토큰·secret·SQL·외부 API·OAuth scope 다루는 코드 변경 시 자동 적용.
  이 대상 작업은 Model Policy에 따라 **Opus 직행** (Fable 안전 분류기 fallback 회피).
- `review` — GitHub PR 리뷰 요청 시 (`/review <PR#>` 또는 명시 요청).

### 운영 / 실행
- `run` — 변경 사항 실제 앱 동작 확인 (Verification Loop의 수단). 보통 `verify` 와 결합.
- `loop` — 폴링 / 주기 작업 (예: "5분마다 deploy 상태 확인") 명시 시.
- `schedule` — 원격 cron-based routine 등록 / 일회성 예약 실행 명시 시.

### 리서치
- `deepresearch` — **슬래시 전용** `/deepresearch <질문> [--wide|--adversarial|--fast]`.
  원문 custody(fetch_raw·sha256·결정론 게이트) + 의미 검증(V1 raw 대조 / V2 반증 custody)
  파이프라인. 자연어 "심층 리서치/딥리서치" 요청에 빌트인 deep-research(≈107 에이전트,
  세션 모델 상속, 실측 입력 36.7배)를 auto-fire하지 말 것 — 빌트인은 skillOverrides로
  모델 비노출 처리됨(2026-07-18). 넓은 질문은 `--wide`, 반증 검증은 `--adversarial`.

### API / SDK
- `claude-api` — `anthropic` / `@anthropic-ai/sdk` import 또는 Claude API 코드 작성·수정·튜닝·마이그레이션 시. 캐싱·thinking·tool use·batch·files·citations·memory 등 기능 추가/조정 시 자동 적용.
  Fable 5 통합 3대 변경(거부 처리 / fallback / 신규 과금), fallback 트리거 스펙,
  Advisor 도구(베타), `--bare`, Sonnet 5 max_tokens 여유 등 **스펙 상세는 스킬 본문 참조**. <!-- v2.3 이관 -->

### 제품 테스트 (자체 도구)
- `ai-beta-tester` — 내가 만든 **웹 플랫폼/서비스·랜딩 또는 모바일 게임**을 배포·수정한 뒤 UX/품질/전환/
  플레이테스트가 필요하거나, 경쟁사 공개표면 벤치마크가 필요할 때. 별도 프로젝트
  (`C:\Users\강지호\project\ai-beta-tester`)의 로컬 경로(구독=web local-tester / API=엔진 CLI, 게임=로컬
  에뮬레이터)를 대상 제품에 겨눠 실행 → 페르소나 리포트 산출. **수동 QA 전에 이 도구를 먼저 제안**할 것.

### 환경 / 설정
- `update-config` — `.claude/settings.json` / `settings.local.json` 변경 (권한, env, hooks, 자동화) 요청 시.
- `keybindings-help` — `~/.claude/keybindings.json` 변경 요청 시.
- `fewer-permission-prompts` — 권한 프롬프트가 반복해서 뜬다는 사용자 불만 또는 명시 요청 시.

### 초기화
- `init` — 새 프로젝트에 CLAUDE.md 자동 생성 명시 요청 시. 생성 시 검증 커맨드
  (test/typecheck/build) 섹션을 반드시 포함시킨다. <!-- v2.3 -->

### Windows 운영 런북 (사용자 스킬 — `~/.claude/skills/`)
2026-05 사고 학습 기반 런북. 트리거 매칭 시 본문이 자동 로드된다.
- `ps-korean-safe-write` — PowerShell + 한글 파일 읽기/쓰기/치환, here-string Python 전달, 한글 깨짐 진단 시.
- `secrets-oauth-ops` — `.env`·비밀키·OAuth client_secret·refresh_token·gitleaks 관련 작업 시.
- `win-task-scheduler` — 작업 스케줄러 등록/수정/수동 검증 (`schtasks`, `*-ScheduledTask`) 시.
- `bat-encoding` — `.bat` 생성/수정, batch→Python 호출, cp949/UnicodeEncodeError 증상 시.
- `service-hang-recovery` — 로컬 서비스(GPU/API) 무응답 감지·복구, 워치독 작성 시.
- `py-oneoff-override` — 정기 작업 스크립트의 일회성 파라미터 override 설계/실행 시. <!-- v2.3 신규 -->

## Agent Routing

기본 원칙: **메인 thread = 오케스트레이터+에디터**. 에이전트는 *작업 분담* 전용 (조율 아님).
병렬 실행·보고 양식 규칙은 Resource & Efficiency 섹션을 따른다.

- `Explore` — 코드 위치 / 정의 / 키워드 grep 같은 **위치 찾기** 단발 (1-3 쿼리). breadth: `quick` / `medium` / `very thorough`. **금지**: 코드 리뷰, cross-file 일관성, 디자인 감사 (read window 제한).
- `Plan` — 비-trivial 구현 전략 / 아키텍처 설계. 단계별 계획 + 영향 파일 + trade-off 필요 시.
- `general-purpose` — Explore 의 단발 grep 으로 부족한 **복잡 멀티스텝 조사** (예: "이 패턴이 repo 전체에 어떻게 퍼져 있는지 + 호출 그래프"). Edit/Write 가능.
- `claude-code-guide` — Claude Code CLI / Agent SDK / Claude API 사용법 질문 한정.
- `statusline-setup` — `~/.claude/settings.json` 의 statusline 설정 변경 요청 시.
- `claude` — 위 어느 것도 매칭 안 되고, 메인 thread 단독으로 처리하기 어려운 복잡도일 때만 사용.

## Decision Behavior
- **명세 게이트 (Plan 선행)** <!-- v2.3 복원 -->:
  non-trivial 구현(신규 기능, 3파일 이상 변경 예상, 스키마·API 계약·아키텍처 영향)은
  Plan 모드 또는 `Plan` 에이전트로 **계획을 먼저 제시하고 승인받은 뒤 구현**한다.
  계획 없이 코드부터 작성하지 않는다. 계획에는 영향 파일 목록·trade-off·검증 방법을
  포함한다. trivial 작업(단일 파일 버그픽스, 오타, 설정 1~2줄)은 게이트 면제.
- 본 문서의 강행 규정(only / 금지 / 무조건)은 Claude Code + Windows 환경을 전제한다.
  전제가 깨진 환경에서 규칙이 현실(도구 가용성·권한·플랫폼)과 충돌하면, 맹목적으로
  준수하거나 무시하지 말고 충돌 사실을 보고한 뒤 의도 기준으로 판단한다.
- 사용자의 질문이 진행 방향 변경 지시가 아닌 단순 호기심일 수 있으므로,
  프롬프트를 맹목적으로 따르지 말고 의도를 파악해 판단한다.
- 작업 도중 모호한 가정이 있으면 진행 중 또는 응답 말미에 명시적으로 질문한다.
- 검증된 자료(공식 문서, 사료, 백서)에 교차검증 후 답한다. Hallucination 금지.
- 사용자 교정으로 실수가 확정되면, 같은 실수의 재발 방지 규칙을 CLAUDE.md 또는
  해당 스킬에 즉시 반영할지 그 자리에서 제안한다 (배치 리뷰 대기 금지). <!-- v2.3 -->

## 세션 규칙 (듀얼 머신 — 중요)
- 세션 시작 시: `PROGRESS.md`를 먼저 읽고 `git status` · `git log --oneline -5`로 실제 상태와 대조한 뒤 이어서 작업할 것
- 세션 종료 시: `PROGRESS.md` 갱신 → 커밋 → `git push`
- `PROGRESS.md`의 머신 표기와 현재 머신이 다르면 먼저 `git pull --rebase` 여부를 확인할 것
- `.env` 등 git에 올라가지 않는 파일은 머신 전환 시 별도로 옮겨야 함
