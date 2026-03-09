뇨핑봇 v9.3 수정 메모

이번 수정:
1. [server] 가 테스트 환영 메시지에서 서버 ID 대신 서버 이름으로 나오도록 보강
2. [channel] 변수를 추가하고 실제 디스코드 메시지에서는 채널 멘션(<#채널ID>)으로 전송
3. 미리보기/이미지에서는 [channel] 이 #채널이름 형태로 표시
4. 환영/퇴장 메시지 템플릿 입력칸을 textarea 로 바꿔 줄바꿈 가능
5. 대시보드에 환영 메시지용 채널 멘션 / 퇴장 메시지용 채널 멘션 선택 추가
6. 봇 쪽 실제 입장/퇴장 메시지에서도 [channel] 적용
7. 봇 DB 마이그레이션에 welcome_message_channel_id / goodbye_message_channel_id 추가

적용 후 해야 할 것:
- GitHub 저장소를 이 ZIP 기준으로 전체 덮어쓰기
- Northflank에서 nyoping-dashboard 재배포
- Northflank에서 nyoping-bot 재배포
- 대시보드에서 자동 인사 설정을 다시 저장
- 테스트 메시지로 [server], [channel], 줄바꿈 확인

확인 예시 템플릿:
환영합니다 [user]
서버: [server]
안내 채널: [channel]
