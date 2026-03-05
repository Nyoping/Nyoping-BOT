Nyoping Dashboard v5.1 DB Fix

문제:
- Postgres의 dashboard_kv 테이블이 예전에 created_at/updated_at 없이 만들어진 상태였습니다.
- v5에서 dashboard_kv.updated_at을 쓰면서 500 (UndefinedColumnError)이 발생.

해결:
- startup 때 아래 migration 실행:
  ALTER TABLE dashboard_kv ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
- 만약 migration이 실패해도, _kv_set이 updated_at 없는 스키마로 fallback 합니다.

적용:
- Render Root Directory 기준으로
  dashboard/main.py 를 이 버전으로 교체 (템플릿은 v5 그대로 써도 됩니다)
- 커밋 후 Render Deploy latest commit

기대 결과:
- /admin/api/roles, /admin/api/members_search가 500이 아니라
  429 + {remaining: N} 형태로 동작하면서, 쿨다운/캐시가 정상 작동합니다.
