뇨핑봇 패치 V13

이번 패치 핵심
1) 반응 역할 취소 시 역할 제거/복구가 안 되던 문제 수정
- 원인: reaction_roles.py 의 on_raw_reaction_remove 리스너가 클래스 밖으로 빠져 있어서 실제로 등록되지 않았음
- 수정: 리스너를 클래스 안으로 복구

2) 환영 메시지/이미지 안정화
- 환영/퇴장 채널을 get_channel 뿐 아니라 fetch_channel fallback 으로도 찾도록 수정
- 전송 성공/실패 로그 강화
- 미리보기 유저 API 추가
- 대시보드 환영 이미지 업로드/미리보기/드래그 편집 JS 복구

3) 대시보드 UI 개선
- 좌측 메뉴 / 우측 설정창 형태로 변경
- 서버 설정 / 빠른 작업 / 레벨 역할 / 반응 차단 / 반응 역할 / 랭킹 탭 분리
- 환영/퇴장/알림/랭킹 채널은 텍스트 채널 드롭다운으로 선택 가능

적용
- Render: nyoping-bot-postgres-render 전체 덮어쓰기 후 재배포
- weirdhost: nyopingbot/ 폴더 전체 덮어쓰기 후 재시작

테스트
- 반응 역할: 반응 추가 후 역할 추가, 반응 취소 후 역할 제거 확인
- 환영 기능: 대시보드 설정 저장 후 실제 입장 테스트, weirdhost 로그에서
  * welcome message sent ...
  * welcome image build failed
  * welcome channel not found ...
  를 확인
