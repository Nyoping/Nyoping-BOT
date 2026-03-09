뇨핑봇 Northflank 전체본 V5

변경점
- 자동 인사 이미지 업로드 후 previewState 오류 방지
- 자동 인사 테스트에서 사진 생성 실패 시 텍스트 fallback 강화
- 테스트 이미지 생성 시 avatar_url이 없어도 배경+텍스트만으로 생성 가능
- 채널 제어 UI에서 '봇/명령어 전용 채널' 항목 제거
- AFK 자동 퇴장 UI 제거 및 봇 로직 비활성화
- 반응 차단 안내문에 '반응 취소 자체는 Discord 구조상 강제 차단 불가' 명시

배포
- 전체 덮어쓰기 후 nyoping-bot, nyoping-dashboard 둘 다 재배포
