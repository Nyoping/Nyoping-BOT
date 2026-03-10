# 뇨핑봇 v1.8

Northflank 기준으로 운영하는 **Discord 봇 + FastAPI/Jinja2 대시보드** 통합 프로젝트입니다.

## 현재 기준 핵심 기능
- 자동 인사 / 퇴장 메시지
- 자동 인사 이미지 생성 및 테스트 전송
- 한글 폰트 포함 이미지 렌더링
- 레벨 / 경험치 / 출석 / 통화 XP
- 프로필 / 랭킹
- 채널 제어 / 반응 기능
- 공개 알림 방식 선택
- Discord OAuth 로그인 기반 서버 선택
- 봇 초대 버튼 포함 대시보드
- 이용약관 / 개인정보처리방침 페이지
- 개발자 정보 메뉴

## 운영 권장 구조
- **봇:** Northflank
- **대시보드:** Northflank
- **DB:** Northflank Postgres

## 필수 환경변수
### 공통
- `DATABASE_URL`
- `DISCORD_TOKEN` 또는 `DISCORD_BOT_TOKEN`
- `PYTHONUNBUFFERED=1`

### 대시보드 관리자
- `DASHBOARD_ADMIN_PASSWORD`
- `DASHBOARD_SESSION_SECRET`

### Discord OAuth 로그인
- `DISCORD_CLIENT_ID`
- `DISCORD_CLIENT_SECRET`
- `DISCORD_OAUTH_REDIRECT_URI`

예시:
```env
DISCORD_OAUTH_REDIRECT_URI=https://너의-대시보드-도메인/oauth/discord/callback
```

중요:
- Discord Developer Portal의 Redirect URI도 **위와 완전히 같은 주소**여야 합니다.
- 예전 `/callback` 주소가 남아 있으면 로그인 오류가 날 수 있습니다.

### 선택 환경변수
- `DISABLE_DISCORD_OAUTH=0`
- `DISCORD_BOT_PERMISSIONS=268823616`
- `DISCORD_INSTALL_SCOPES=bot applications.commands`

### 법적 / 개발자 정보
- `DEVELOPER_NAME`
- `SUPPORT_SERVER_URL`

## Discord Developer Portal 설정
### Bot
- Public Bot: ON
- 필요한 Intent:
  - Server Members Intent
  - Presence Intent(필요 시)
  - Message Content Intent(필요 시)
  - Voice State 관련 기능 사용 가능 상태

### OAuth2 / Installation
- Redirect URI:
  - `https://너의-대시보드-도메인/oauth/discord/callback`
- Guild Install Scopes:
  - `bot`
  - `applications.commands`

## 배포 순서
1. GitHub에 프로젝트 업로드
2. Northflank에 `nyoping-dashboard`, `nyoping-bot`, Postgres 구성
3. 환경변수 등록
4. `nyoping-dashboard` 배포
5. `nyoping-bot` 배포
6. Discord OAuth 로그인 테스트
7. 서버 선택 → 봇 초대 → 설정 저장 테스트

## 대시보드 주요 경로
- `/` : 메인 화면
- `/admin` : 관리자/서버 설정 화면
- `/oauth/discord/callback` : Discord 로그인 콜백
- `/legal/privacy` : 개인정보처리방침
- `/legal/terms` : 이용약관

## 개발자 정보
- 개발자: 뇨핑 nyoping
- 지원겸 종합게임 서버: https://discord.gg/SUq8a4j4xB

## 정리
예전 버전에서 생성된 `README_PATCH_*.txt` 파일들은 제거하고,
현재 프로젝트 기준 설명은 이 `README.md` 하나로 통합했습니다.
