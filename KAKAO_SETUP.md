# 카카오톡 "나에게 보내기" 설정 체크리스트

Kakao Developers 콘솔(https://developers.kakao.com)에서 아래 순서대로 설정하면
본인 계정으로 "나에게 보내기"를 **앱 심사 없이** 사용할 수 있습니다.
(talk_message 권한은 앱을 만든 본인 계정에 한해 심사 없이 사용 가능합니다. 타인에게
메시지를 보내려면 별도 검수·팀원 등록이 필요하지만, "나에게 보내기"에는 불필요합니다.)

문서 확인 기준일: 2026-07. 메뉴 명칭은 콘솔 개편에 따라 달라질 수 있습니다.

## 1. 애플리케이션 생성
- 콘솔 로그인 후 **내 애플리케이션 > 애플리케이션 추가하기**로 앱을 만듭니다.
- 앱 이름 · 사업자명을 입력하고 저장합니다.

## 2. REST API 키 확보
- **내 애플리케이션 > (앱 선택) > 앱 설정 > 앱 키** 로 이동합니다.
- **REST API 키** 값을 복사합니다.
- 프로젝트 루트에서 `.env.example`을 `.env`로 복사하고
  `KAKAO_REST_API_KEY=` 뒤에 이 값을 붙여넣습니다.

## 3. 카카오 로그인 활성화
- **제품 설정 > 카카오 로그인** 으로 이동합니다.
- **활성화 설정**을 **ON**으로 켭니다.

## 4. Redirect URI 등록
- 같은 **카카오 로그인** 화면 하단 **Redirect URI** 에
  `http://localhost:8888/callback` 을 **정확히 그대로** 등록합니다.
  (kakao_auth.py의 콜백 서버 주소와 일치해야 하며, 경로·포트가 다르면 실패합니다.)

## 5. talk_message 동의항목 활성화
- **제품 설정 > 카카오 로그인 > 동의항목** 으로 이동합니다.
- **접근권한 관리** 또는 목록에서 **카카오톡 메시지 전송(talk_message)** 항목을 찾습니다.
- 상태를 **사용** 으로 설정합니다(본인 사용은 "선택 동의"로 충분).
  - 이 항목이 목록에 없으면 **비즈니스 > 카카오 로그인 접근권한**에서 활성화가 필요할 수
    있습니다. 단 "나에게 보내기"는 본인 계정 사용이라 앱 검수 없이 동작합니다.

## 6. (선택) Client Secret
- **앱 설정 > 보안** 에서 Client Secret을 **사용함**으로 설정한 경우에만,
  발급된 코드값을 `.env`의 `KAKAO_CLIENT_SECRET=` 에 넣습니다.
- 사용 안 함이면 이 항목은 비워 둡니다(코드가 자동으로 생략).

## 7. 토큰 발급 (1회)
```
py -3 kakao_auth.py
```
- 브라우저가 열리면 카카오 로그인 후 **talk_message** 동의를 완료합니다.
- 완료되면 `.env`의 `KAKAO_REFRESH_TOKEN`이 자동 저장됩니다.
- refresh token 유효기간은 **60일(5184000초)** 입니다. 이 값이 폐기/만료되면
  (`invalid_grant`) 위 명령을 다시 실행해 재발급합니다.
  - 자동 전송을 60일 이상 지속하면 refresh token이 주기적으로 갱신됩니다. 코드가
    갱신된 refresh token을 `.env`에 자동 회전 저장하므로 별도 조치는 불필요합니다.
    (카카오는 refresh token 잔여 유효기간이 1개월 미만일 때만 새 값을 함께 발급합니다.)

## 8. 전송 테스트
```
py -3 kakao_sender.py --check              # 키 설정 상태만 마스킹 진단
py -3 kakao_sender.py --test "테스트 메시지"   # 실제로 나에게 전송
```

## 설정값 요약
| 항목 | 값 |
| --- | --- |
| Redirect URI | `http://localhost:8888/callback` |
| scope | `talk_message` |
| 인가 URL | `https://kauth.kakao.com/oauth/authorize` |
| 토큰 URL | `https://kauth.kakao.com/oauth/token` |
| 전송 API | `POST https://kapi.kakao.com/v2/api/talk/memo/default/send` |
| 템플릿 | text (본문 최대 200자, 초과 시 자동 분할) |
| access token 유효 | 약 6시간 (자동 갱신) |
| refresh token 유효 | 60일 (자동 회전) |
