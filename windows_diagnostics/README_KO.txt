태산이 PC 실행·진단 패키지 v2

1. ZIP 전체를 압축 해제합니다. ZIP 안에서 바로 실행하지 마세요.
2. 카카오톡에 로그인한 뒤 기획방을 별도 창으로 엽니다.
3. START_DIAGNOSE.cmd를 실행합니다. Python 설치나 관리자 권한은 필요하지 않습니다.
4. 결과가 메모장으로 열립니다. 창을 닫아도 SHARE_THIS_STATUS.txt에 남습니다.

이 도구는 기획방 창과 입력창 후보의 기술 정보만 점검합니다.
대화 본문, 입력 중인 초안, 클립보드는 읽지 않으며 클릭·입력·전송·네트워크 업로드를 하지 않습니다.
다른 고객 방은 수집 대상으로 열지 않습니다. 카카오톡 창 제목은 정확히 기획방인지 비교할 때만 사용합니다.
입력창 후보가 발견되어도 원격 접속이나 주문 자동화가 연결된 상태는 아닙니다.
실제 Windows 실행은 회사/집 PC에서 별도 확인해야 합니다.

상태 안내
kakaotalk_not_running: 카카오톡을 실행하고 로그인한 뒤 다시 진단합니다.
planning_room_not_open: 기획방을 별도 창으로 엽니다.
planning_room_ambiguous: 같은 이름의 기획방 창을 하나만 남기고 직접 확인합니다.
planning_room_minimized: 기획방을 복원한 뒤 다시 진단합니다.
input_control_not_exposed: 카톡 입력창이 UI 자동화에 노출되지 않아 다른 수집·전송 방식 검토가 필요합니다.
input_candidates_need_review: 후보를 찾았지만 실제 입력창인지 수동 검토가 필요합니다. 전송한 상태가 아닙니다.
diagnostic_error: 같은 폴더 또는 TEMP/TaesanDiagnostics의 결과를 확인합니다. 조직 보안 정책을 임의로 바꾸지 마세요.

공유가 필요하면 SHARE_THIS_STATUS.txt만 사용하세요.
diagnostic-detail-local.txt는 오류 상세를 로컬에 보관하며 경로 등 개인 정보가 포함될 수 있습니다.
진단 후 기존 주문방·확인방이나 운영 시트를 변경하지 마세요. 다음 단계도 사본과 기획방 테스트로 제한합니다.
