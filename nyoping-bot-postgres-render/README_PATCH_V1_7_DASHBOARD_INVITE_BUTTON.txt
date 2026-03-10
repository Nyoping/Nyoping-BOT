뇨핑봇 v1.7 패치 메모

이번 수정:
- 대시보드 첫 화면에 "봇 초대하기" 카드 추가
- 관리자 /admin 서버 선택 영역에 "봇 초대하기" 버튼 추가
- 선택된 서버가 있으면 "이 서버에 봇 초대" 전용 링크 표시
- Discord 설치 링크는 DISCORD_CLIENT_ID 또는 DISCORD_APPLICATION_ID 기준으로 생성
- 권한값은 DISCORD_BOT_PERMISSIONS 환경변수(기본 268823616) 사용

필요 환경변수:
- DISCORD_CLIENT_ID=Discord Application ID
또는
- DISCORD_APPLICATION_ID=Discord Application ID

선택 환경변수:
- DISCORD_BOT_PERMISSIONS=권한 정수값
- DISCORD_INSTALL_SCOPES=bot applications.commands
