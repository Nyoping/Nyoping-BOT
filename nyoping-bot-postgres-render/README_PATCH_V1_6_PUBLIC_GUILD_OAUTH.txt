뇨핑봇 v1.6-public (v1.5 기준)

이번 수정:
- Discord OAuth 로그인 추가
- 다른 서버 관리자도 자기 서버만 선택 가능
- 서버별로 "연결됨 / 초대 필요" 표시
- 봇이 없는 서버는 바로 초대 링크 제공
- 기존 관리자 비밀번호 로그인 유지

필수 환경변수:
- DISCORD_CLIENT_ID
- DISCORD_CLIENT_SECRET
- DISCORD_OAUTH_REDIRECT_URI
- DASHBOARD_SESSION_SECRET
- DATABASE_URL

선택 환경변수:
- DISCORD_BOT_PERMISSIONS=268823616
- DISABLE_DISCORD_OAUTH=0  (또는 제거)

중요:
- Developer Portal에서 Installation / OAuth2 Redirects 에
  DISCORD_OAUTH_REDIRECT_URI 와 같은 주소를 등록해야 함
- 예시:
  https://너의대시보드도메인/oauth/discord/callback

권장 흐름:
1. Public Bot 켜기
2. Guild Install에 bot + applications.commands 사용
3. Dashboard 첫 화면에서 Discord 로그인
4. 자기 서버 선택
5. 초대 필요면 초대 후 새로고침
