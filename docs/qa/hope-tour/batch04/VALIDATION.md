# 최종 검증 기록 — 2026-11-16

- 실행 코드: `d7cc1b5fc474a8822108ad44af315211f3ef232a`.
- 실제 서비스 후보: `6f0d7fc1a7a0b328edaa36568a8522edc86e196f`; 원본 Site 버전33 `9433bf650183d9b8c7efe437b1f0f002bfcc0677`.
- 실행: `python scripts/run_hope_batch04.py --source /workspace/work/hope-source-batch04 --state-dir /workspace/work/hope-batch04-state-delivery --output docs/qa/hope-tour/batch04/run-20261116`.
- 개발20개 generated / 16 executed·asserted / 16 pass / 0 fail / 4 unmeasured. 4개는 실제 브라우저 조작·화면·UI 교차·MEM-10 최종 재시험.
- 중앙 result API: **blocked**, 사유 `critical_scenario_not_passed`, `required_coverage_missing`. 예상대로 CLI 종료1. 바인딩·source·profile·실행영수증 불일치 오류는 없음. 종료코드만으로 성공을 판정하지 않는다.
- 최종 전체 회귀: `python -m unittest discover -s tests -q`, **1,756 실행 / 1,755 통과 / 1 skip / 실패·오류0**, 162.661초. 로그는 비공개 작업 공간 `hope-batch04-delivery-regression.log`.
- 최종 집중: `python -m unittest tests.test_hope_batch04 tests.test_hope_batch03 tests.test_hope_batch02 -q`, **31개 통과**. 이전19개와 새12개이며 위 전체 회귀와 합산하지 않는다.

## 실제 실행 증거

- 독립 network-none Docker 컨테이너2개가 실제 회원 준비 후 같은 SQLite barrier에 모두 도달. release 뒤 같은 마지막1석 경쟁에서 확정1·거절1. source SHA·역할·컨테이너명·개별 API/재고 관찰 보존. 같은 이벤트루프 Promise 두 개와 구분.
- 별도 합성 회원 계약/검수 공간의 동시 신청도 성공1·거절1·확정1. 실제 공급사나 운영 회원 검증으로 표현하지 않는다.
- 객실2개 예약의 부분취소 재고1 → 동일 반환 재전달 뒤1 → 나머지 취소 뒤2, 다시2객실 예약 성공. 실제 포인트·카드·반환 이벤트와 원장 대사 오류0.
- 예약·납부 요청 재전달 뒤 예약1·양수 카드승인1. 구세대409. 활성/uncertain shared lease가 남으면 fixture 재준비 거절.
- 여행 예약금·잔금 양수 청구에서 응답 불명 후 서로 다른 요청번호 재청구 둘 다409. 조회 전 pending 영수증을 실제로 읽어 각각1개 확인. 조회 전 취소/만료409, 관리자 대조 후 잔여 납부·환불·원수단/공유 재고 복원.
- 순수 저장소 상태 전이: 조건부 차감 실패·known 만료 반환·uncertain 만료 거절. 시간만 경과해 불확실 효과를 해제하지 않음. 범용 재시작 후 uncertain 원거래 복구 UI는 후속 작업.
- 실제 별도 Python 관리기 프로세스를 중지·시작하여 file SQLite의 owner/worker 인증 해시·프로필·job·보고서·산출물 회수. raw 토큰은 부모 메모리에만 유지하고 DB/환경/인자/로그에 넣지 않음.
- 재시작된 worker가 running probe job을 보면 reconciliation_required, 재선점409, 실제 실행/프로브 호출0. 이 probe는 업무 코드를 실행하지 않은 별도 프로토콜 fixture이며 owner가 그 사실을 대조 후 종료.
- 프로토콜 산출물의 바이트 변조·행 삭제는 seal/list/get에서409. 다른 프로젝트403/404, 8MiB 초과 반입 거절, seal 후 파일 추가·변경 거절. 실제 최종 보고서10개를 seal 후 관리기 프로세스를 다시 시작하고 HTTP 회수·원본 SHA 대조 모두 통과.

## 독립 계산·오류 주입

- 사전 고정 TEST 재고10개 자원: `available + sum(quantity-released) = capacity` 일치. worker가 보고한 capacity도 별도로 승인 계약 수량과 대조.
- 다섯 실제 예약의 DP/BP/SP/CP/카드25개 값이 독립 고정 기대값과 일치. 이는25개 사용자 시나리오 수가 아니다. 대상 함수를 호출해 정답을 만들지 않으며, U04/U10 원금·무료 혜택·상품가·BP10%·가입비 제외·TEST 순서를 별도 계약/변수/정수 규칙으로 기록.
- 실제 정상 결과들의 원장 book별 합계0·sourceKey 유일성·원수단별 반환 한도·카드 납부청구/반환 이벤트·수수료0 전액취소의 완전 복원 확인. 부분취소와 조회 전후/환불 상태도 포함.
- 주입4종 독립 검출4/4: 실제 공유DB free 수량 변조(관찰 후 롤백), 실제 결과 복제본의 균형 있는 중복 차감·초과 반환·불균형 원장. 상태 복제 주입을 운영 장애라고 표현하지 않는다.
- 대상 자체 대사기는 중복 차감을 놓쳤음(`target_detected=false`); 독립 대사기는 `duplicate_journal_source`로 검출. `BUG-RECONCILE-DUPLICATE-001`, P0/S1 `OPEN_TEST_DETECTION_GAP`로 별도 보고. 실제 운영 중복 차감 발생이 입증된 것은 아니다.
- 추가 검사에서 균형을 유지한 과소반환·대상 계산계획 변조·카드 반환 이벤트 불일치·승인 capacity 변경도 검출.

## 최종 포맷·제한

- 고객38개 시나리오 행 유지. PASS0/FAIL0/BLOCKED8/NOT RUN30. 이번 소스에서 실행하지 않은 PAY-05 카드거절 등에는 이전 SHA의 결과를 재사용하지 않음.
- 기존 원금 정책·용어·실제 공급사 미연결 결함과 새 대사기 검출 공백을 보존. 미종료 S1/S2는3개. 고객 인수·운영 승인 불가.
- Word/Excel 재열기, 계산식·오류 주입·61–80 개발 시트, 전체 SHA256SUMS 대조, 원본에 후보 패치 `git apply --check`, `git diff --check` 통과.
- 원시 JSON/gzip·Word/Excel XML·패치에 공개 합성 로그인 암호가 남지 않았음을 확인. 세션·setup/암호화 키·관리기 원문 토큰은 산출물로 전달하지 않음.
- 관리기와 재고 DB는 전용 state 디렉터리에 보존하며 Git/보고서에는 반입하지 않음. 실제 review 업무 DB는 각 컨테이너의 메모리 DB라는 범위도 유지. 영속 업무 DB와 범용 원거래 복구는81–100 제안에 포함.
- 실제 공급사·PG·공개 사이트 브라우저·외부 중앙 서버는 미연결/미실시. 운영 배포·Site source push·실제 결제·발송·운영자료 변경·유료 자원 생성 없음.
- rootless Docker의 cgroup 하드 메모리 제한 미지원, 로컬 SQLite D1 호환 계층 한계, TEST 수수료0·포인트 순서 등 운영 정책 승인 미확정은 그대로 보존.
