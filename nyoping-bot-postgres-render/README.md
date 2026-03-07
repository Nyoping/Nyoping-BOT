# 뇨핑봇 (공개 배포 + 대시보드) — Postgres 버전

이 ZIP은 **봇(Discord)** 과 **대시보드(Web)** 가 같은 **Postgres(DB)** 를 공유하도록 만든 버전입니다.

## 추천 구성
- **봇**: weirdhost / VPS / 내 PC (24시간 가능 환경)
- **대시보드**: Render Web Service
- **DB**: Neon Postgres 같은 외부 Postgres

## 0) 준비물
- Discord Developer Portal 앱(봇)
- Postgres 연결 문자열(DATABASE_URL)
- Render에 배포할 GitHub 저장소(이 프로젝트 업로드)

---

## 1) DB (Neon) 만들기
1. Neon에서 프로젝트 생성
2. Connection string 복사
3. `.env` 또는 Render Env Vars에 `DATABASE_URL`로 등록

> 스키마는 봇이 처음 실행할 때 자동으로 생성됩니다.

---

## 2) Discord 설정
### Bot 탭
- Token 생성 → `DISCORD_TOKEN`
- Intents:
  - Server Members Intent ✅ (역할 지급/관리)
  - Voice States ✅
  - Message Content Intent: XP 지급은 내용이 필요 없지만, 환경에 따라 켜두면 안전

### OAuth2 (대시보드)
- OAuth2 → General → Redirects:
  - `https://<Render_주소>/callback`
- 대시보드가 요청하는 scope: `identify guilds`

---

## 3) Render 배포(대시보드)
Render → New → Web Service → GitHub repo 연결

- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn dashboard.main:app --host 0.0.0.0 --port $PORT`
- Env Vars:
  - DATABASE_URL
  - DASHBOARD_BASE_URL (Render가 준 URL)
  - DISCORD_CLIENT_ID
  - DISCORD_CLIENT_SECRET
  - DASHBOARD_SESSION_SECRET

---

## 4) 봇 실행(weirdhost/PC)
### Windows PowerShell (로컬)
```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
.\.venv\Scripts\python -m nyopingbot
```

### weirdhost/Pterodactyl
- Startup의 PY_FILE을 `bot_entry.py`로 두고 실행하면 됩니다.
- requirements.txt는 자동 설치됩니다.
- 환경변수(.env 또는 패널 변수)에 DISCORD_TOKEN/DATABASE_URL을 넣어주세요.

---

## 5) 슬래시 명령어
- /checkin : KST 기준 출석(하루 1회, 설정으로 제한 OFF 가능)
- /profile : 내 XP/레벨/출석
- /leaderboard : TOP 10
- /settings ... : XP/쿨다운/테스트옵션
- /levelrole ... : 레벨 도달 시 역할 추가/제거
- /clean : 최근 메시지 N개 삭제

