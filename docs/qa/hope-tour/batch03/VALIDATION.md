# 최종 검증 기록 — 2026-11-16

- 전체 회귀: `python -m unittest discover -s tests -q`, **1,744 실행 / 1,743 통과 / 1 skip / 실패·오류0**, 158.965초. 실행 로그는 비공개 작업 공간 `hope-batch03-release-regression.log`. 이 회귀 후 어댑터의 표시 암호 가림과 양수 카드거절 조건을 보강했고, 아래 최종 격리 실행 및 집중 검사를 수행했다. 숫자를 서로 합산하지 않는다.
- 최종 집중 검사: `python -m unittest tests.test_hope_batch03 tests.test_hope_batch02 -q`, **19개 통과**. 이전8개 포함·새11개.
- 최종 실행 코드: `38ae6066a38be787e2b332a5b7520122283fbfbe`.
- 실제 서비스 수정 후보: `eaacbf0b9a999171e8d288f2e72b3bc3536ca973`. 원본은 Site 버전33 `9433bf650183d9b8c7efe437b1f0f002bfcc0677`.
- 실행: `python scripts/run_hope_batch03.py --source /workspace/work/hope-source-batch03 --output docs/qa/hope-tour/batch03/run-20261116`.
- 기대대로 종료1: 관리기 실제 HTTP/QAWorker/QAJobs 결과 **blocked**. 인수 거절 사유 `critical_scenario_not_passed`, `required_coverage_missing`. source/profile/receipt/job 바인딩 불일치 오류는 없음.
- 개발20개 생성·18개 실행/단언·17개 일치·1개 실패·2개 미검증. 실패는 전역 재고 공유를 검증할 수 없는 검수 환경 한계. 미검증은1280px 및390/360px 실제 브라우저.
- 실제 소스 BP9개 조합: 실제 도메인 예약·납부·원장 잔액 차이·카드 납부 합계·확정 상태 일치. 독립 BP 정수식9개 일치. 단일 CASE56의 하위 조합 수로 보고한다.
- 실제 카드 거절: U05의 **300,000원** 테스트 예약금 청구에 failed 처리, 잔액 손실 없음. 금액0인 승인을 거절 사례로 집계하지 않는다.
- 최초 납부 응답 불명 후 다른 요청 재청구409, 관리자 대조 후 pending0. 여행 예약금·잔금 응답 불명은 별도 후속 범위.
- 같은 검수 공간 동시신청 합계1건. 서로 다른 검수 공간은 각1건 총2건이므로 전역 공유 재고 인수 조건 실패. 운영 초과판매가 입증되었다는 주장이 아님.
- 실제 관리기 HTTP: 5종 바인딩 변경·필수항목 삭제·기대값 변조 거절. 동일 작업 중복·프로젝트 실행한도·취소 후 재실행 거절. owner 등록 해제·worker 등록 거절. 소스/어댑터 변경 fencing은 추가 검사에서 확인.
- 실제 격리 컨테이너 중단 probe: 중단 후 자식/컨테이너 정리 확인. 취소 상태 전이는 별도 무효과 관리기 프로토콜 fixture에서 검증하고 서로 구분한다.
- 최종 보고서10개를 owner 전용 반입 경로로 저장 후 실제 HTTP로 회수, 파일 바이트SHA 전부 대조. 다른 프로젝트 접근 거절·같은 이름 덮어쓰기 거절. 임시 관리기 DB는 실행 종료 시 폐기.
- Word/Excel 재열기·고객38행 유지 확인. 최종 고객 판정 **PASS0 / FAIL1 / BLOCKED12 / NOT RUN25**.
- 원본의 용어 누락과 후보의9개 전체 명칭을 독립 HTML 본문으로 재시험. native details 구조/설명 확인은 실제 키보드·터치 브라우저 실행 증거와 별도다.
- 공개 합성 로그인 암호가 원시 JSON/gzip·Word/Excel XML에 남지 않았는지 확인. 입력란과 안내표는 저장 전 가림. 세션/관리기 인증키/서비스 암호화키는 보고서에 전달하지 않는다.
- 전체 SHA256SUMS 재대조, 후보 패치의 원본 적용 가능성 `git apply --check` 통과, `git diff --check` 통과.

산출물은 `run-20261116/`. 실제 PG·실기기·공개 사이트 브라우저·외부 중앙 서버는 미연결/미실시. 운영 배포·실제 결제·발송·운영자료 변경·유료 자원 생성 없음. rootless Docker의 cgroup 하드 메모리 제한은 지원되지 않음.
