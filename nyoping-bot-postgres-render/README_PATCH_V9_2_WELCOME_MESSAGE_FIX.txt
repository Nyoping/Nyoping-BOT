뇨핑봇 패치 메모 - V9.2 welcome message fix

이번 패치 내용
1) 자동 인사 변수 [server]
- 대시보드 테스트 환영 메시지에서 가능한 한 서버 이름을 그대로 사용하도록 보강
- 봇 실제 입장 메시지는 guild.name 기준으로 계속 서버 이름 사용

2) 환영/퇴장 메시지 줄바꿈
- 대시보드 입력칸을 textarea로 변경
- Enter로 줄바꿈한 내용이 그대로 저장되고 디스코드 메시지에도 그대로 전송됨

3) 채널 멘션 변수 [channel]
- 자동 인사/퇴장 메시지에 [channel] 변수 추가
- 대시보드에서 "메시지용 채널 멘션" 드롭다운으로 채널 선택 가능
- 실제 디스코드 메시지에서는 채널 멘션으로 전송됨
- 대시보드 미리보기/테스트 이미지에서는 #채널이름 형태로 표시됨

수정된 주요 파일
- dashboard/main.py
- dashboard/templates/admin.html
- nyopingbot/cogs/community_features.py
- nyopingbot/db/pg.py

적용 방법
- 이 전체 프로젝트본 기준으로 GitHub에 덮어쓰기
- Northflank에서 dashboard / bot 둘 다 재배포
- 재배포 후 자동 인사 설정에서 저장 한번 수행
  (새 DB 컬럼 welcome_message_channel_id, goodbye_message_channel_id 마이그레이션 반영용)

권장 테스트
1) 환영 메시지 템플릿에 아래처럼 입력
환영합니다 [user]
서버: [server]
안내 채널: [channel]

2) 메시지용 채널 멘션에서 원하는 채널 선택
3) 테스트 환영 메시지 전송
4) 실제 입장 테스트도 같이 확인
