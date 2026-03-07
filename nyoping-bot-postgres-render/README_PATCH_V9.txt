뇨핑봇 패치 V9

이번 패치 내용
1) 반응 역할 이모지 별칭 지원
- 대시보드에서 :snowflake: 같은 별칭으로 저장해도 실제 유니코드 이모지로 정규화되게 수정
- 봇도 DB에 저장된 emoji_key를 정규화해서 비교하도록 수정
- current rules 표시는 emoji_display 기준으로 출력

2) 반응 역할 디버그 로그 추가
- 규칙 매칭 여부
- 역할 추가/제거 성공/실패
- 관리 가능한 역할이 없을 때 로그 출력

3) 출석 제한 OFF 테스트 모드 강화
- 같은 날 여러 번 출석하면 streak 증가
- streak bonus 설정이 0이어도 테스트 모드에서는 최소 10XP/일 보너스가 보이도록 수정

적용 방법
- Render: nyoping-bot-postgres-render 전체 덮어쓰기 후 재배포
- weirdhost: nyopingbot/ 폴더 전체 + requirements.txt 덮어쓰기 후 재시작
- 기존 반응 역할 규칙은 삭제 후 다시 저장 권장
  (특히 이전에 :snowflake: 같은 별칭으로 저장해둔 규칙)

테스트 팁
- 반응 역할 입력은 여전히 실제 이모지(예: ❄️)를 권장하지만, :snowflake: 도 이제 동작하도록 보강함
- weirdhost 로그에서 다음 문구를 확인하면 됨
  * Reaction role: matched ...
  * Reaction role: added roles ...
