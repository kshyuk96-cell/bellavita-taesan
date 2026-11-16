# 승인 묶음81–100: 업무 TEST DB 영속·원거래 조회 복구·하위 증거 계약

후보 소스와 로컬 TEST 환경의 검증이다. 실제 공급사·PG·브라우저·외부 중앙 서버·운영 배포는 미적용. 모든 결제는 simulator이며 고객 최종 인수는 차단한다.

## 재현

- QA 브랜치 `codex/hope-qa-batch05-20261116`, #85 위에 쌓는 PR.
- 원본 Site 버전33 `9433bf650183d9b8c7efe437b1f0f002bfcc0677`, 이전 후보 `6f0d7fc1a7a0b328edaa36568a8522edc86e196f`.
- 이번 후보의 전체 SHA는 `deploy/hope-batch05/source-profile.json`. 로컬 `codex/qa-recovery-reconcile-candidate-20261116`; Site source push·게시 없음.
- 실행: `python scripts/run_hope_batch05.py --source /approved/clean/candidate --state-dir /approved/fresh/test-state --output /tmp/fresh-batch05-report`.
- 고정 Docker Node24 digest 사전 준비, 실행 코드 먼저 커밋, 새 state/output 필요. 기존 state에 같은 CLI를 다시 실행하지 않는다. 중단 시 state를 보존하고 명시적 owner 대조를 수행한다.
- 원본부터 누적 후보 패치는 `run-20261116/source-candidate.patch`. 다른 SHA로 커밋하면 그 소스로 프로필·계약을 새로 승인한다. 이전 통과 결과를 새 SHA에 재사용하지 않는다.
- 기존 QAJobs/QAWorker의 실제 비운영 HTTP claim 하나 안에서 전체 source 실행/중단/조회/CAS를 수행한다. 결과는 사전 계약으로 판정하며 현재 종료1은 인수 차단이다.

## 영속·복구 계약

업무 `/business/review.sqlite`, 공유 재고 `/exchange/inventory.sqlite`, 관리기 `manager.sqlite`를 분리한다. 업무 DB에는 실제 원래 review 스키마·fixture·source/task/contract 메타데이터가 있다. 각 source Worker는 같은 파일 DB에서 원래 Worker fetch/rvCommand/rvApply를 실행한다. D1 자체가 아닌 SQLite 호환 계층이다.

영속 binding은 workspace·task·source·business digest·generation·run·slot·기존 요청ID를 묶는다. 각 operation에는 원래 command fingerprint·실행자·저장 전 revision·진행 지점을 보존한다. 요청이 workspace/role/task를 임의로 선택할 수 없다. 등록은 승인된 host fixture 경로이며 공개 요청으로 허용하지 않는다.

`POST /review/qa-recovery/v1/query`는 본인 세션·세대와 기존 requestId만 받아 원거래를 읽는다. 네이티브 command를 실행하거나 시간을 근거로 재고를 해제하지 않는다. `.../reconcile`은 실제 owner TEST 역할과 별도 fixture key가 모두 필요하다. executor 종료·컨테이너 부재의 host 관찰 증명, source/task/generation, 업무 원거래 fingerprint를 대조한 뒤에만 연결을 복원한다.

업무 요청/예약이 없고 업무 revision도 저장 전과 같아야 미실행으로 판정해 확보를 해제한다. 기존 예약이 있으면 그 ID를 연결하며 book을 재호출하지 않는다. 응답 불명 결제가 있으면 연결을 복원해도 재고는 uncertain으로 보류하고 native 원거래 조회가 필요하다. 진행 중 실행자, 다른 task/source/generation, 다른 workspace, 원거래 불일치에는 복구를 거절한다. 조회 요청 자체가 업무 revision·원장·예약을 바꾸지 않는지도 대조한다.

새 코드의 fixture 세션 재개는 승인된 합성 workspace에 한해 세션 원문을 메모리에서 생성하고 native session 해시만 DB에 저장한다. 실제 운영 사용자의 인증 복구 검증이 아니다. 공개 JSON command 경로만 지원하며 bound TEST의 form command 우회는405로 거절한다. 이 controller는 공개 사이트에 설치하지 않았다.

REVIEW_DATA_KEY는 QA job 수명 동안 부모 메모리에서 유지해 각 컨테이너 stdin으로만 전달한다. config·명령행·보고서·Git·로그에 넣지 않는다. setup은 암호화 sentinel을 업무 DB에 저장하고, 이후 Worker는 원래 ciOpen으로 복호화해 같은 source/task/key 맥락인지 확인한다. DB에는 키가 아닌 ciphertext만 남는다. CLI 부모 자체 종료 후 키를 복구하는 외부 secret broker는 미연결이며, DB만 복사해 전체 암호화 업무 복구가 완료됐다고 주장하지 않는다. 새 CLI는 기존 state를 자동 재실행하지 않는다.

## 실제 중단·동시성

5지점(`before-hold`, `after-hold`, `before-store`, `after-store`, `before-response`)에서 source 프로세스가86으로 실제 종료된다. host가 프로세스 종료와 컨테이너 부재를 확인하고 DB의 phase·revision·요청 기록·lease 상태를 읽어 지점이 맞는지도 검증한다. 자식의 완료 답변이나 종료코드만 채택하지 않는다. 복구 Worker는 별도 새 컨테이너다.

업무 저장 전3건은 원거래 부재를 입증하고 해제·동일 요청 재실행409. 저장 후2건은 기존 예약1건만 복원. 단계별 pending 원거래와 source fingerprint도 보존한다. 별도 결제 응답불명은 재시작 후 다시 청구하지 않고 조회→확정→취소→재조회 거절까지 검증한다.

독립 Worker 둘은 같은 workspace·같은 요청·같은 업무 revision에서 pay를 계산한 뒤, 실제 SQLite의 조건부 UPDATE 바로 전에 TEST barrier로 멈춘다. 둘 다 준비됐음을 host가 확인해야 CAS 경쟁을 시작한다. 실제 source의 rvCommand는 한 UPDATE만 성공시키고 다른 요청은 저장된 동일 요청 결과로 반환한다. barrier는 D1 호환 계층의 테스트 계측이며 원래 업무 함수 본문을 바꾸지 않는다. 실제 Cloudflare D1 동시성이나 실제 PG의 exactly-once 보장은 별도다.

## 계산·대사·고객 계약

원래 자체 대사기에 journal sourceKey, 공급자 event ID/key 중복 검출을 추가한 소스 후보를 제공한다. 잔액이 양수인 균형 중복 차감도 검출한다. 중복 이벤트·초과 반환·원장 누락은 대상과 독립 오라클 각각 판정을 기록한다. 이전 후보의 실제 검출 누락 증거는 역사적 실패로 보존하며 새 소스의 통과 결과로 재사용하지 않는다.

부분취소는 정상가515,470원×2명·회원할인3%·최종1,000,011원·2객실이다. 독립 Fraction 오라클로 원 단위 비례 배분과 최대 나머지, 같은 나머지면 객실 인덱스 우선 규칙을 검사한다. 첫 객실은 잔금 전 취소, 두 번째는 잔금 후 취소하고 동일 반환 재전달도 검사한다. 사용 순서와 객실 슬라이스 배분 순서는 구분하고, CP/SP/BP/DP/card 슬라이스 순서·수수료0·BP/SP 상대 순서는 TEST 가정으로 명시한다. 운영 정책 승인으로 승격하지 않는다. 취소로 해제된 미납 카드금액은 실제 카드 반환액에 합산하지 않는다.

고객38행마다 필수 하위 증거를 사전 고정한다. 프로젝트·작업·후보·테스트계약·어댑터·세대에 모두 맞아야 하며, 결제 중복 API를 가입 시험에 붙이는 등 다른 기능 매핑도 거절한다. API 하나만 통과하면 전체 workflow는 BLOCKED다. browser/PG/정책/공급사 등 미등록 capability를 worker가 JSON에 표시해도 거절한다. 중요 행 삭제·중요도 하향·다른 source/세대도 거절한다. 전체 행은 분모38에서 빠지지 않는다.

## 20개 매트릭스

|번호|개발·관찰|판정 범위|
|---|---|---|
|81|분리된 영속 업무DB·source/task/schema·키 복호화 맥락|새 Worker에서 같은9 workspace 확인|
|82|workspace/task/source/generation 영속 binding|변경·외부workspace 조회 거절|
|83|기존 requestID readonly 조회|6회 revision/상태 불변·member 복구 거절|
|84|저장된 원예약 연결 복구|저장후2건 연결·결제불명1건 보류|
|85|미실행 증명 뒤에만 확보 해제|저장전3건 해제·재실행 거절·실행중 복구 거절|
|86|5개 실제 종료·저장 체크포인트|프로세스/컨테이너 종료 및 네이티브 기록 대조|
|87|독립 Worker2·동일 workspace/revision CAS|UPDATE1성공/1실패·카드/원장1회|
|88|응답불명 조회·취소·재조회 교차 재시작|재청구409·반환20만원·재조회409|
|89|양수 잔액의 중복 source 원장 검출 수정|원본 누락/후보 검출 별도 보존|
|90|중복 이벤트·초과 반환·부족 원장|대상/독립 각각3종 검출·정상0|
|91|지원 CUA 준비 계약|도구 없음 skip·fallback 없음|
|92|키보드/터치 실제 용어 조작|미검증|
|93|실제360/390/1280px 화면|미검증|
|94|UI/API/원장/재고 실제 교차|미검증|
|95|38행 필수 하위 증거|부분 API를 전체PASS로 승격 금지|
|96|다른SHA/작업/기능/세대·하향/누락|10종 거절 프로브|
|97|홀수 총액·객실별 정수 나머지|독립 계산식·원수단 대조|
|98|잔금전후·동일 반환 재전달|미납해제100005/실제반환100005/재고2|
|99|미승인 정책과 TEST 가정 분리|고객 인수 차단 유지|
|100|결함 재시험·고객38행·Word/Excel|실행 증거와 누락 계약 함께 회수|

개발20개는 사용자 시나리오 전체 수가 아니다. 5개 중단 지점·2개 CAS 실행자·개별 계산 값은 하위 증거로 별도 기록하고20개나 기존 수천 합성 건수와 합산하지 않는다. 고정 가상시각·시나리오를 쓰며 UUID·동시성 승자는 바뀔 수 있으나 불변식과 원거래 연결로 재현한다.

## 남은 범위

[Sites SKILL.md](skill://plugin_connector_1p_689987207de08191979cf68eca2941c6/sites/SKILL.md)의 “If `cua_repl.js` is unavailable, skip browser QA”를 적용한다. 도구 부재로 preview 실행/브라우저 설치/임의 대체 조작은 하지 않았다. 92–94와 실제 공급사/PG/정책·외부 중앙 서버는 미검증/미연결로 유지한다. 실제 공개 서비스의 수정을 게시하거나 운영 계정·결제를 변경하지 않는다.

기존 pinned Docker network-none/read-only source/UID65534/Node permission/CPU·파일·시간·출력 제한을 유지하고 승인된 business/exchange만 쓰기 마운트한다. rootless Docker cgroup 하드 총메모리 제한 미지원은 그대로다. source Worker 종료가 부모 QA controller 전체 장애의 자동 복구까지 입증하지 않는다. 다음101–120은 그 범위와 key broker·tenant 권한·백업복원을 먼저 제안한다.

현재 부재 판정은 정상적으로 보존된 TEST 업무 DB를 전제로 한다. DB를 과거 백업으로 되돌리거나 임의 삭제한 뒤 같은 revision만 보고 안전한 미실행으로 판정하는 운영 복원 절차는 지원하지 않는다. rollback/교체 witness와 두 DB의 일관된 백업복원은101–120의 우선 항목이며, 고객 OPS-01은 실행하지 않은 상태로 유지한다.
