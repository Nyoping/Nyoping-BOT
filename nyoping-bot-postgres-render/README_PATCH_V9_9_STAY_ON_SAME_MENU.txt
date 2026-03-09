[v9-9] 저장 후 현재 메뉴 유지 수정

변경 내용
- 대시보드에서 저장/적용/규칙 저장/반응 저장 후에도 현재 보고 있던 메뉴로 다시 돌아오도록 수정
- 자동 인사 / 채널 / 반응 / 레벨 역할 메뉴 모두 반영
- 메뉴 상태를 return_pane, return_group 으로 폼에 실어서 리다이렉트에 같이 전달
- /admin 첫 로드 시 URL의 pane, group 값을 읽어서 해당 메뉴를 다시 열도록 수정

수정 파일
- dashboard/main.py
- dashboard/templates/admin.html

적용 방법
1. GitHub 저장소를 이 ZIP 기준으로 전체 덮어쓰기
2. Northflank에서 nyoping-dashboard 재배포
3. 저장 버튼 테스트

예시
- 자동 인사 메뉴에서 저장 -> 자동 인사 메뉴 유지
- 채널 메뉴에서 저장 -> 채널 메뉴 유지
- 반응 메뉴에서 저장 -> 반응 메뉴 유지
