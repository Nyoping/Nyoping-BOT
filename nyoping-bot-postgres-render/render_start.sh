#!/usr/bin/env bash
set -e

echo "[nyoping] DB 보정 시작"
python render_db_fix.py || true
echo "[nyoping] 앱 시작"

if [ -n "$NYOPING_ORIGINAL_START_COMMAND" ]; then
  echo "[nyoping] 사용자 지정 시작 명령 실행"
  exec bash -lc "$NYOPING_ORIGINAL_START_COMMAND"
fi

if [ -f "dashboard/main.py" ]; then
  exec python -m uvicorn dashboard.main:app --host 0.0.0.0 --port "${PORT:-10000}"
fi

if [ -f "main.py" ]; then
  exec python -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-10000}"
fi

echo "[nyoping] 시작 명령 자동 감지 실패"
echo "[nyoping] Environment에 NYOPING_ORIGINAL_START_COMMAND 를 추가하세요."
exit 1
