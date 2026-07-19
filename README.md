# vibecoding — 커뮤니티 인기 글 카카오톡 다이제스트

바이브 코딩/Claude Code 커뮤니티들의 **최근 72시간 인기 글**을 수집해
카카오톡 "나에게 보내기"로 발송하는 자동 루틴. (구축: 2026-07-18)

## 스케줄 (GitHub Actions — PC 상태 무관)

- 워크플로: `.github/workflows/digest.yml` — 매일 00:00 UTC(=09:00 KST) 트리거 +
  epoch-day % 3 게이트로 정확한 3일 주기 (기준 정렬: 2026-07-21부터)
- 시크릿: GitHub repo Secrets (`KAKAO_REST_API_KEY` / `KAKAO_CLIENT_SECRET` /
  `KAKAO_REFRESH_TOKEN` / `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` / `ADMIN_TOKEN`(선택))
- 토큰 회전: 러너에서 새 refresh token 수신 시 `ADMIN_TOKEN`(PAT)이 있으면 Secret 자동
  갱신, 없으면 카카오로 수동 갱신 안내 발송
- 수동 실행(로컬): `py -3 main.py` (전송 포함) / `py -3 main.py --dry-run` (전송 없이 확인)
- 수동 트리거(클라우드): Actions 탭 → community-digest → Run workflow
- 소요: 약 5~6분 (Reddit rate limit 간격 대기 포함)
- (구) Windows 작업 스케줄러 `VibecodingCommunityDigest`는 Actions 검증 후 제거됨

## 구성 요소

| 파일 | 역할 |
| --- | --- |
| `communities.json` | 수집 대상 정의 — **커뮤니티 추가/제거는 이 파일만 수정** |
| `collect.py` | 수집기 4종: `reddit` / `hn_algolia` / `rss` / `dcinside` (커뮤니티 단위 실패 격리) |
| `report.py` | 한국어 보고문 조립 |
| `main.py` | 오케스트레이션 (수집→보고→전송) |
| `kakao_sender.py` | 카카오 "나에게 보내기" (200자 분할, refresh token 자동 회전) |
| `kakao_auth.py` | OAuth 재인증 CLI (`invalid_grant` 시 1회 실행) |
| `KAKAO_SETUP.md` | 카카오 콘솔 설정 체크리스트 |

## 수집 대상 (2026-07-18 딥리서치로 확정)

해외: r/ClaudeCode, r/ClaudeAI, r/vibecoding, HN(쿼리 "Claude Code"/"vibe coding"), Lobsters vibecoding 태그
국내: 긱뉴스(GeekNews) — 리서치 결과 **국내 유일의 개방형 수집 가능 채널**
제외 사유 기록: 클리앙(robots 쿼리 차단·RSS 없음), 네이버 카페(로그인벽), 카카오 오픈챗(수집 경로 없음),
X(무료 API 없음), Discord(ToS 스크래핑 금지), OKKY(피드 없음), 디시(밀도 미달 — 수집기는 존재, enabled=false)
근거 리포트: `~/Documents/research/vibe-coding-communities/research.md`

## 운영 시 알아둘 것

- **이 네트워크에서 Reddit `.json`은 403** — 수집은 `.rss` 폴백(점수 없음, top 순서 유지)으로 동작.
  점수·정확한 랭킹이 필요하면 https://www.reddit.com/prefs/apps 에서 script 앱 생성 후
  `.env`에 `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` 추가 (OAuth 모드로 자동 전환).
- 카카오 refresh token 유효 60일, 루틴이 3일마다 갱신·회전하므로 정상 운영 중엔 만료 없음.
  장기 중단 후 `invalid_grant` 에러가 로그에 보이면 `py -3 kakao_auth.py` 재실행.
- 시크릿은 전부 `.env` (커밋 금지). 진단은 `py -3 kakao_sender.py --check` (마스킹 출력).
