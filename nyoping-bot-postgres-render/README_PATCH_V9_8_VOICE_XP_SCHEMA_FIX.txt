v9-8 voice xp schema fix

문제:
- 레벨 설정 저장 시 Internal Server Error
- 원인: guild_settings 테이블에 voice_xp_enabled / voice_xp_interval_min / voice_xp_amount / voice_xp_daily_cap / voice_xp_block_delay_min 컬럼이 없는 DB에서, 대시보드가 바로 UPDATE를 시도함

수정:
- dashboard/main.py
  - 누락된 voice xp 컬럼 ALTER TABLE migration 추가
  - 대시보드 시작 시 migration 실행
  - 설정 조회/저장 직전에도 runtime schema ensure 실행
- nyopingbot/db/pg.py
  - 봇 시작 시 동일한 voice xp 컬럼 migration 추가

적용:
1. 이 ZIP으로 GitHub 전체 덮어쓰기
2. Northflank에서 nyoping-dashboard 재배포
3. Northflank에서 nyoping-bot 재배포
4. 레벨 설정 다시 저장

정상 결과:
- Internal Server Error 없이 저장됨
- 기존 DB에도 새 voice xp 설정 컬럼이 자동 생성됨
