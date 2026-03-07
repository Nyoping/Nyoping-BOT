Northflank 대시보드 admin 로그인/서버목록 핫픽스 V3

이번 패치:
- /admin 로그인 직후 guild_id가 없을 때 guild_name 계산 중 int(None)로 500 나던 문제 수정
- 서버 목록 수집 시 asyncpg.Record를 dict로 안전 변환하도록 수정
- guild_name 계산을 _safe_selected_guild_name() 헬퍼로 분리

적용:
- Northflank 대시보드 서비스 코드에 덮어쓰기
- 커밋/푸시 후 nyoping-dashboard 재배포
