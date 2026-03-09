뇨핑봇 패치 V12.1 - 서버 선택 500 완화

이번 패치는 /admin 에서 guild_id 선택 직후 Internal Server Error 가 나는 문제를 완화합니다.

핵심 수정
- dashboard/main.py의 admin 페이지를 "옵션형 조회"로 변경
- 일부 테이블/컬럼이 아직 생성되지 않았어도 빈 목록으로 계속 화면 진입 가능
- members_search / members_list / members_by_role 도 avatar_url / in_guild / role_ids 컬럼이 없어도 fallback 조회로 동작
- Render 로그에 원인 파악용 경고 로그 추가

적용
- Render 프로젝트에 nyoping-bot-postgres-render 전체 덮어쓰기
- Render 재배포

적용 후에도 500이 나면 Render 로그에서 다음 문구 근처를 보내주세요.
- Admin page open guild_id=
- Optional query failed
- Failed to load guild settings
