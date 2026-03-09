뇨핑봇 v1.5 패치 메모

이번 수정:
- 자동 인사 이미지 업로드 버튼 다시 동작하게 수정
- 자동 인사 테스트용 메시지 보내기 버튼 다시 동작하게 수정
- 환영 이미지 미리보기 JS 복구
- 미리보기 대상 유저 hidden input에 name 추가

원인:
- v1.4 UI 정리 과정에서 자동 인사 이미지 업로드/미리보기/테스트에 필요한
  JavaScript 함수 블록이 admin.html에서 빠져 있었음
- 그래서 uploadWelcomeImage, sendWelcomeTest, refreshWelcomePreview, previewState 관련 함수가
  브라우저에서 정의되지 않아 버튼이 동작하지 않았음

적용:
1. GitHub 저장소를 이 ZIP 기준으로 전체 덮어쓰기
2. Northflank에서 nyoping-dashboard 재배포
3. 자동 인사 메뉴에서
   - 이미지 업로드
   - 미리보기 새로고침
   - 테스트용 메시지 보내기
   를 다시 확인
