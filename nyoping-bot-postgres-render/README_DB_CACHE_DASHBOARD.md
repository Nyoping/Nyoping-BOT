# 대시보드 Discord 레이트리밋 해결 (DB 캐시 방식)

Render 공유 IP에서 Discord REST(/roles, /members/search)가 30분 이상 레이트리밋되는 문제를 해결하기 위해,
대시보드가 Discord API를 직접 호출하지 않고 **봇이 DB에 캐시한 데이터**를 읽도록 변경했습니다.

## 바뀐 점
- 대시보드 `/admin/api/roles`, `/admin/api/members_search`는 Discord API를 호출하지 않습니다.
- 역할 목록: `guild_roles_cache` 테이블에서 읽습니다.
- 유저 검색: `guild_members_cache` 테이블에서 읽습니다.
- 봇이 시작될 때(ready) 역할/멤버 캐시를 DB에 동기화하고, 메시지/통화/출석 때마다 유저 캐시를 업데이트합니다.

## 필요한 것
- 봇이 실행 중이어야 캐시가 갱신됩니다.
- 유저 검색은 캐시에 있는 유저만 나옵니다. (활동한 유저는 자동으로 캐시에 쌓입니다)

## 적용
이 ZIP을 기준으로 GitHub에 반영 후 Render에 배포하세요.
