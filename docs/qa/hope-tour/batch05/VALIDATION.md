# 최종 검증 기록 — 2026-11-16

- 실행 코드 `536b1357571f10092ff9c235347762b9b0eda740`.
- 실제 후보 `6e31be85187fec2e5b8592ec3e8e7cb2535352e3`, 이전 후보 `6f0d7fc1a7a0b328edaa36568a8522edc86e196f`, 원본 Site 버전33 `9433bf650183d9b8c7efe437b1f0f002bfcc0677`.
- 실행: `python scripts/run_hope_batch05.py --source /workspace/work/hope-source-batch05 --state-dir /workspace/work/hope-batch05-state-release --output docs/qa/hope-tour/batch05/run-20261116`.
- 개발20개 generated, 17 executed/asserted/pass, fail0, unmeasured3(92–94 실제 브라우저). 관리기 실제 result API는 blocked, 사유 `critical_scenario_not_passed`, `required_coverage_missing`; 예상 종료1. Source/profile/job/실행영수증 불일치 오류는 없음.
- 전체 회귀 `python -m unittest discover -s tests -q`: **1,768 실행 / 1,767 통과 / 1 skip / 실패·오류0**, 170.153초. 비공개 작업 로그 `hope-batch05-regression.log`.
- 집중 `python -m unittest tests.test_hope_batch05 tests.test_hope_batch04 tests.test_hope_batch03 tests.test_hope_batch02 -q`: **43개 통과**, 이전31개와 새12개. 전체와 합산하지 않는다.

## 영속·중단·동시 실행

- 승인된 전용 file SQLite 업무 DB와 공유 재고 DB 분리. setup이 만든9개 workspace를 새 Worker가 그대로 조회. source/task/contract/schema/generation 메타데이터 확인.
- 한 QA claim 안에서 source 컨테이너11개 실행: setup1, 제어 종료5, recovery1, CAS Worker2, unknown 완료1, main1. 11개 실행자가11개 사용자 시나리오라는 의미는 아님. 별도 고정 confinement readiness probe는 source 시나리오로 집계하지 않음.
- 5개 지점에서 실제 exit86·프로세스 종료·컨테이너 부재를 host가 확인. inventory operation phase와 업무 DB를 종료 직후 직접 읽어 요청 기록이 `[없음, 없음, 없음, 있음, 있음]`인지 대조. 역할 문자열이나 exit code만 채택하지 않음.
- 저장 전3건은 원거래/예약 없음·동일 업무 revision·종료 증명으로 확보 해제, 원래 요청 재실행409. 저장 후2건은 원예약 ID만 연결·예약수1, book 네이티브 command 재호출 없음.
- 읽기 전후6건의 업무 revision과 예약/원장 상태 동일. member 복구403, 다른 workspace404, 다른 source/task/generation409, 실행중으로 표시된 executor의 복구409.
- 독립 Worker 둘이 같은 workspace·requestID·revision에서 계산 완료 후 실제 conditional UPDATE 직전 barrier 진입. CAS changes `[0,1]`, 양수 카드 승인1·booking journal1. 나머지는 실제 rvCommand의 저장된 동일 요청 결과 반환.
- 업무 DB에 남은 암호화 sentinel을 새 source Worker가 원래 ciOpen으로 복호화해 key/source/task 맥락 확인. 키는 QA job 부모 메모리에서 컨테이너 stdin으로만 전달. config/명령행/DB/보고서/로그/Git에 원문 저장 없음.
- 재시작된 결제불명 공간에서 재청구409, 관리자 원거래 조회 후 취소·카드20만원 반환, 재조회409. native source와 공유 재고/원장 대조 오류0.

## 대사·계산·증거 계약

- 이전 후보 실제 중복 원장 주입은 대상 검출 false. 이번 후보는 같은 full duplicate와 잔액이 양수인1원 균형 중복 차감 모두 검출. `BUG-RECONCILE-DUPLICATE-001`은 `FIXED_IN_LOCAL_CANDIDATE`와 이번 SHA로 기록하고 게시했다고 표현하지 않음.
- 대상/독립 오라클이 중복 이벤트·초과 반환·부족 원장3종 각각 검출. 중복 source2종까지 합한 주입5개 모두 대상·독립 검출. 실제 정상 상태 대사 오류0.
- 정상가515470×2·회원할인3%·최종1000011·객실합계 `[500006,500005]`. 독립 유리수·최대 나머지 오라클은 BP `[50001,50000]`, 카드 `[100005,100005]`를 확인. 21개 scalar 기대/실제 값 일치이며21개 사용자 시나리오가 아님.
- 첫 객실 잔금 전 취소로 미납100005 해제, 실제 카드 반환0. 나머지 잔금100005 납부 뒤 두 번째 취소로 실제 카드100005 반환. 동일 반환 새 요청409·공유 재고2 복원. 미납 해제를 카드 반환으로 더하지 않음. 부분취소/수수료0/슬라이스 순서는 TEST 가정이며 운영 승인 대기.
- 고객38행의 필수 하위 증거 계약 보존. 부분 API 한 개로 전체 workflow PASS 금지. 6개 바인딩(project/task/source/test contract/adapter/generation), 다른 기능 매핑, 미등록 browser capability, 중요 행 삭제, priority 하향의10종 프로브 거절. 추가 검사에서 bool generation·중복 증거·PG/정책 capability 자기 선언도 거절.
- 최종 고객 판정 **PASS0/FAIL0/BLOCKED6/NOT RUN32**. 이전 SHA나 실행하지 않은 가입/탈퇴/카드거절 증거는 연결하지 않음. 기존 정책·용어·실제 공급사 미연결 조건을 보존하고 인수 차단. 미종료 S1/S2는2개, 수정된 자체 대사기 항목은 로컬 후보 수정으로 별도 분류.

## 산출물과 제한

- Word/Excel 재열기·고객38행·독립 계산식·개발81–100·필수하위증거 시트 확인. 새 고객 하위 증거 계약 포함 실제 번들11개를 seal하고 관리기 프로세스 재시작 후 HTTP로 모두 회수·SHA 대조.
- SHA256SUMS 전부 재대조, 합성 로그인 암호가 JSON/gzip·Word/Excel XML·패치에 남지 않았는지 확인. 원본에 누적 source patch `git apply --check`, `git diff --check` 통과. runtime key는 CLI가 실제 값을 알고 있는 시점에 원시 증거에 미포함인지 추가 검사.
- 업무/재고/관리기 DB는 private state에 보존하고 Git/보고서에는 넣지 않음. 세션 원문은 메모리, native session hash만 저장. TEST fixture 인증 재개를 실제 운영 사용자 인증 복구로 주장하지 않음.
- source Worker 재시작을 검증했으며 부모 controller 전체 장애·외부 key broker·과거 백업 rollback/교체 뒤의 두 DB 일관성은 미검증. 정상 DB 보존을 벗어난 복원에 revision만으로 안전한 부재 판정을 적용하면 안 됨. 다음101–120에 포함하고 OPS-01은 NOT RUN 유지.
- 실제 공급사/PG/브라우저/외부 중앙 서버 미연결. Site source push·운영 배포·실제 결제/발송·운영 자료 변경·유료 자원 생성 없음. SQLite D1 호환 계층과 rootless Docker cgroup 하드 총메모리 제한 미지원 한계 유지.
