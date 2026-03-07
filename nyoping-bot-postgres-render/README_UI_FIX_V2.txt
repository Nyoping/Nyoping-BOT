뇨핑봇 Render 대시보드 UI 핫픽스 v2

이 ZIP은 dashboard/templates/admin.html 1개만 들어 있습니다.

고친 내용
1. 닫는 script 태그 오타(/script>) 수정 -> 유저 검색/역할 검색 JS가 다시 실행되도록 수정
2. 레벨 역할 규칙의 현재 규칙에서 역할 이름이 ?로 보이던 문제 완화
   - role_name_map 키가 문자열/정수 어느 쪽이어도 최대한 이름이 보이게 템플릿 보정
3. 반응 역할 추가/제거 카드 UI 추가
   - 특정 메시지 + 특정 이모지 기준으로 역할 추가/제거 폼 표시
4. 검색 API 실패 시 빈 목록으로 안전 처리

적용 방법
- Render 프로젝트의 dashboard/templates/admin.html 을 이 ZIP 안의 파일로 덮어쓰기
- 재배포
- /admin 접속 후 아래를 확인
  1) 유저 검색 클릭 시 목록 표시
  2) 레벨 역할 규칙에서 역할 검색 목록 표시
  3) 현재 규칙에서 역할 이름 표시
  4) 반응 역할 추가/제거 카드 표시

주의
- 이 ZIP에는 백엔드(Python) 수정은 없습니다.
- 만약 반응 역할 저장 버튼을 눌렀을 때 404가 나면, 실제 서버 라우트 이름이
  /admin/reaction-role-upsert /admin/reaction-role-delete 와 다른 경우입니다.
  그때는 전체 프로젝트 ZIP을 보내주면 백엔드까지 맞춰서 다시 묶어드릴 수 있습니다.
