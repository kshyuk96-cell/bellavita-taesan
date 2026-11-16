# 승인 묶음61–80: 공유 TEST 재고·여행 결제 불명·영속 관리기

실제 공급사·실제 PG·브라우저·운영 배포는 미적용이다. QA 공유 재고 어댑터는 실제 후보 Worker 앞의 명시적인 비운영 경로다. 공개 서비스에 공유 재고가 연결되었다고 표현하지 않는다.

## 재현

- 원본 Site 버전33: `9433bf650183d9b8c7efe437b1f0f002bfcc0677`.
- 이전 용어 후보: `eaacbf0b9a999171e8d288f2e72b3bc3536ca973`.
- 이번 후보: `6f0d7fc` 전체 SHA는 `deploy/hope-batch04/source-profile.json` 참조. 로컬 소스 브랜치 `codex/qa-shared-inventory-candidate-20261116`; Site 저장소 push·게시 없음.
- QA 브랜치 `codex/hope-qa-batch04-20261116`, #83 위에 쌓는 PR.
- 실행: `python scripts/run_hope_batch04.py --source /approved/clean/candidate --state-dir /approved/fresh/nonproduction-state --output /tmp/fresh-batch04-report`.
- 정확한 pinned Node24 Docker 이미지가 사전 준비되어 있어야 한다. 자동 pull·호스트 대상 코드 실행·사용자 명령 실행 없음.
- 코드 먼저 커밋. 새 state와 output 폴더만 허용하며 기존 run을 다시 실행하지 않는다. 중단된 state는 보존하고 owner 대조 절차를 밟는다.
- state에는 실제 파일 기반 관리기 SQLite와 TEST 재고 SQLite가 남고, 인증 원문은 저장하지 않는다. 종료된 예제 CLI의 raw token은 메모리에서 소멸한다. 이후 조회가 필요하면 승인된 로컬 bootstrap으로 별도 owner 연결을 발급한다. 상태 DB를 Git/보고서에 넣지 않는다.
- `run-20261116/source-candidate.patch`는 원본부터 누적 수정이다. dirty 패치 적용 상태는 실행 불가. 별도 SHA로 커밋하면 해당 소스/프로필/계약을 다시 승인한다.

## 구조와 안전

`inventory-contract.json`은 프로젝트·후보SHA·adapter버전·소유주체·상품P02·출발일·객실·시나리오별 수량을 사전 고정한다. 별도 exchange 폴더의 `inventory_*` 테이블만 사용한다. 실제 review D1은 기존 메모리 DB와 분리된다. 계약 digest와 SQLite 메타데이터를 대조하며, 독립 관리기는 보고된 capacity를 신뢰하지 않고 사전 계약 수량과도 대조한다.

공유 재고는 `BEGIN IMMEDIATE` 안에서 조건부 차감·요청키 불변·상태전이·객실별 반환 PK를 처리한다. 만료된 시간만으로 불명확한 효과를 자동 해제하지 않는다. 실제 Worker 호출 전에 uncertain으로 fencing하고, 원거래가 확인된 경우에만 예약에 연결한다. 별도 DB 두 개를 하나의 분산 트랜잭션이라고 주장하지 않는다. 불확실 2xx/5xx·중단·확인되지 않은 연결은 보류하며 자동 보상·자동 재시도하지 않는다. 아직 uncertain 예약을 원거래 조회로 복원하는 범용 운영 UI는 제공하지 않는다.

`sharedGateway`는 인증된 실제 세션의 검수 공간·세대·상품/출발일/객실과 재고 요청을 묶는다. 역할·금액·세션 검사는 실제 Worker에 그대로 위임한다. 활성/uncertain 재고가 남으면 seed 재설정을 막는다. 결제 완료·취소·만료 후에도 실제 원장을 읽어 확인된 효과만 공유 재고에 반영한다.

후보 `review-server.mjs`는 여행 청구의 pending-query가 있으면 같은 청구에 대한 다른 요청ID 재납부를409로 차단한다. 등록 카드 시나리오를 바꿔도 우회할 수 없다. 해당 예약 취소·반환 실행과 pending 상태의 만료도 먼저 조회하도록 차단한다. 실제 PG 호출은 없다.

독립 실행자 둘은 동일 exchange 파일을 마운트한 별도 network-none Docker 컨테이너다. 두 실행자가 실제 회원 준비를 마친 뒤 barrier에 도달해야 관리기가 경쟁을 시작한다. 같은 JS 이벤트루프의 Promise 두 개를 독립 실행자 증거로 집계하지 않는다. 각 실행자의 소스SHA·컨테이너명·역할·개별 결과를 보존한다.

격리는 기존 pinned Node Docker/읽기전용 소스/UID65534/cap삭제/Node권한/CPU·파일·시간·출력 제한을 유지한다. 변경점은 검증된 전용 exchange 폴더만 `/exchange`로 쓰기 허용하는 것이다. 일반 호스트 FS·자식 실행·외부 네트워크는 금지한다. rootless Docker cgroup 하드 총메모리 제한 미지원과 로컬 SQLite D1 호환 한계는 그대로다. 실제 Cloudflare D1·공급사·PG 검증이 아니다.

## 관리기 영속 설정과 재시작

`persistent-host.example.json`은 배포 명령이 아닌 비운영 구성 예제다. trusted bootstrap이 Store에 owner/worker 해시를 등록하고, owner만 `HopeRegistry.provision` 및 사전 계약을 승인한다. 작업이 임의 코드·경로·이미지를 지정할 수 없다. 프로필 해제·소스 dirty·어댑터 변경은 fencing된다. 기존 release의 fingerprint 교체는 금지하며 새 버전으로 등록한다.

`hope_local_manager`는 이미 존재하는 전용 DB에서 loopback HTTP만 시작하고, readiness에는 포트만 저장한다. `PersistentManager`로 실제 별도 프로세스를 중지·재시작한다. raw 인증 토큰을 환경/인자/로그/DB에 저장하지 않고 부모 메모리에 유지한다. running job을 재시작된 worker가 발견하면 실행하지 않고 reconciliation_required로 남긴다. 프로토콜 restart probe는 고객 코드나 외부 효과를 발생시키지 않았으므로 그 사실을 별도로 대조해 owner가 종료한다.

산출물은 owner host 반입만 허용한다. 기존 파일별 hash/크기/권한·덮어쓰기 금지에 더해 `seal`한 묶음의 파일명/크기/hash 목록도 고정한다. 재시작 후 누락·추가·바이트 변조는 list/get에서 거절한다. 파일8MiB·반입24MiB 제한, 다른 프로젝트 접근 금지를 검사한다. 최종 실제 Word/Excel/원시 증거 묶음을 seal하고 관리기 프로세스를 다시 시작한 뒤 HTTP로 모두 회수한다. `manager-recovery.json`은 그 회수 후 생성한 별도 영수증으로 묶음 밖에서 SHA256SUMS와 함께 제공한다.

## 브라우저·인수

[Sites SKILL.md](skill://plugin_connector_1p_689987207de08191979cf68eca2941c6/sites/SKILL.md)의 “If `cua_repl.js` is unavailable, skip browser QA” 지침에 따라 도구가 없으면 preview/브라우저 설치/대체 조작을 하지 않는다. 준비71은 skip 계약으로 검사하되, 실제 키보드/터치72·화면73·UI/API/재고74·MEM-10 최종 재시험75는 미검증으로 남긴다. 정적 HTML 용어는 별도로 대조하며 브라우저 결함을 자동 종료하지 않는다.

고객38개 행과 정책·공급사/실제PG·실기기·화면 조건은 유지한다. 개발 묶음의 테스트 통과를 고객 최종 인수 PASS로 승격하지 않는다. 이전 공유재고 한계는 TEST 어댑터 검증 범위에서만 개선됐고 실제 공급사 인수는 계속 보류한다.
