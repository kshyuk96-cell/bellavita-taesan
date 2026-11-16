# 승인 묶음 41–60: 용어 수정 후보·실제 소스 예외·중앙 관리기 연결

**고객 인수 차단.** 개발항목17개 일치 / 1개 실패 / 2개 미검증. 기존 묶음의 합성 수천 건이나 전체 회귀 수를 이 개발항목 수와 합산하지 않는다.

## 재현과 산출물

- QA 브랜치: `codex/hope-qa-batch03-20261116`, #81 위에 쌓는 PR.
- 실제 서비스 원본: Site 버전33, `9433bf650183d9b8c7efe437b1f0f002bfcc0677`.
- 수정 후보: `eaacbf0b9a999171e8d288f2e72b3bc3536ca973`, 로컬 `codex/qa-terms-candidate-20261116` 브랜치. 실제 Site 저장소 push·게시 없음.
- `run-20261116/source-candidate.patch`를 원본에 적용하고 동일 Git 커밋을 재현하거나, 다른 SHA로 검토·승인한 새 프로필/계약을 등록한다. 패치만 적용한 dirty 체크아웃은 실행 거절한다. 후보 SHA를 속여 원본 통과 결과를 재사용하지 않는다.
- 실행: `python scripts/run_hope_batch03.py --source /approved/clean/candidate --output /tmp/hope-batch03-new-output`.
- 프로필의 정확한 Docker Node digest가 승인된 호스트에 이미 있어야 한다. 실행기는 이미지 pull·셸 명령 입력·호스트 대상 실행 fallback을 제공하지 않는다.
- 실행 코드 먼저 커밋, 새 결과 폴더 필요. 현재 종료코드1은 인수 차단을 뜻한다. 성공 여부는 중앙 보고서의 사전 계약·커버리지 판정으로 확인한다.
- `actual-source-evidence` / `common-report` / `trusted-contract` / `batch-summary` / 고객 Word·Excel·JSON / 독립 BP 계산식 / 원본·후보 용어 재시험 / 패치 / 중앙 HTTP 회수 목록·체크섬 제공.

## 20개 매트릭스

|번호|구현·검증 범위|판정|
|---|---|---|
|41|공통 화면과 금액 명칭 DP/BP/SP/CP 전체 명칭|HTML 일치|
|42|PG/API/CRM/QA/KPI 공통 용어 사전|HTML 일치|
|43|native details/summary 9개·설명·44px·focus-visible|구조 일치, 실제 키보드/터치 조작 미실시|
|44|고정 후보·뷰포트·행동·증거의 지원 브라우저 계약|도구 부재 시 skip/no fallback 일치|
|45|1280px 실제 UI/API/원장 대조|미검증|
|46|390/360px 실제 화면·넘침·터치|미검증|
|47|원본과 후보의 MEM-10 전체 명칭 독립 HTML 재시험|원본 누락/후보9개 일치, 결함 최종 종료는 보류|
|48|owner 프로필 등록·해제·동일버전 불변·worker 등록 금지|일치|
|49|실제 QA HTTP 접수·claim·고정 소스 어댑터 실행|로컬 비운영 통합 일치|
|50|프로젝트·작업·후보·승인계약·어댑터 바인딩|5종 다른 결과 연결 거절|
|51|동일 요청 같은 job·프로젝트 실행1개·재실행 거절|일치|
|52|취소 불확실 상태·재실행 거절·로컬 자식 중단/회수|일치|
|53|권한검사·불변 산출물·Word/Excel HTTP 왕복|일치; 최종 실제 번들10개도 전부 회수|
|54|필수항목 삭제·expected 변조 결과 제출|중앙 result API에서 차단|
|55|U05/U06/U07 실제 결제·0/충분/1원 원금 경계|일치|
|56|실제 도메인 예약·납부·잔액/카드 3등급×3상품 BP9조합|독립 정수 기대값 일치|
|57|카드 거절·동일 요청 동시처리·내용 충돌|일치|
|58|최초 결제 응답불명 재청구 차단·관리자 대조|일치; 여행잔금 응답불명은 별도 후속 범위|
|59|마지막1석 같은 공간과 다른 공간의 동시성|FAIL: 같은 공간1건, 서로 다른 공간은2건|
|60|취소 후 원수단 복원·1석 재예약·구세대 거절|일치|

개발항목 generated20/executed18/asserted18/pass17/fail1/unmeasured2. BP9조합은 실제 rvApply 예약·납부를 실행하고, 사전/사후 잔액 차이와 납부 카드합계까지 독립 대조한다. 56번의 하위 관찰이며 전체20개와 합산하지 않는다. 48–54는 실제 관리기 HTTP 코드의 별도 프로토콜 fixture로 먼저 검사하고, 최종 job은 다시 실제 서비스 후보 코드를 Docker 실행한 뒤 결과를 중앙 result API에 제출한다. 간단한 프로토콜 fixture를 고객 결제·서비스 시험 결과로 표현하지 않는다.

## 고객 최종 판정

38개 시나리오 행 유지. PASS0 / FAIL1 / BLOCKED12 / NOT RUN25. 일부 API/HTML 확인은 화면·PG·정책 승인이 필요한 최종 시험을 PASS로 승격하지 않는다. 용어는 후보 HTML9개 명칭이 확인되었지만 실제 브라우저의 키보드·터치·가독성은 미검증이므로 결함을 자동 종료하지 않는다. 이전 원금 우선 관련 S1도 UI/정책 확인 전 보류한다.

`LIMIT-REVIEW-INVENTORY-001`: 실제 검수 서버의 재고는 검수 공간마다 독립이다. 한 공간에서는 실제 Worker CAS로 동시신청 합계1건을 확인했으나, 별도 공간 두 곳은 각각1건씩 예약했다. 이는 **운영 서비스에서 초과판매가 발생했다는 증거가 아니며**, 이 검수 경로가 고객의 전역 마지막1석 조건을 증명할 수 없다는 한계다. 공유 공급사 비운영 재고 어댑터가 연결되어야 한다.

## 연결·격리와 안전

`HopeRegistry`는 owner만 프로비저닝하고 host가 제공한 소스 경로·이미지·adapter 버전·각 파일 해시를 불변 등록한다. 비활성화, 소스 dirty/SHA 차이, 어댑터 변경은 resolve/heartbeat에서 거절한다. 작업이 임의 경로·명령을 전달할 수 없다. 기존 QAWorker/QAJobs 및 `/v1/qa/jobs`를 사용한다. 임시 SQLite 관리기 + loopback HTTP에서 전 경로를 실제 실행했고 **외부 중앙 서버 설치·연결은 아직 미적용**이다.

관리기 승인: project/source/contract/coverage/adapter/profile digest를 owner가 사전 등록. job은 approval/task/request에 묶이며 기존 accepted 결과의 인수 검증을 재사용한다. 후보/작업 등 바인딩을 바꾸면 차단한다. 등록 해제는 실행 fencing에 반영된다. 취소·타임아웃·전달 불명은 reconciliation_required로 남기고 자동 재시도하지 않는다. 중단 probe는 실제 격리 컨테이너 실행을 중단하고 컨테이너·자식 정리를 확인하며, 관리기 취소 상태 검사는 별도의 무효과 프로토콜 job에서 수행한다.

`GET /v1/qa/jobs/{id}/artifacts` 및 `.../artifacts/{name}`은 프로젝트 권한과 저장된 바이트 해시를 확인한다. base64 파일·이름·크기·SHA 반환. 파일 반입은 host owner의 `HopeArtifacts.collect`만 가능하며, 허용된 보고서 이름/일반 파일만, 파일당8MiB/반입24MiB 제한. 심볼릭 링크·임의 경로·동일 이름 덮어쓰기 거절. 최종 실제 번들10개를 저장 후 HTTP로 모두 회수하고 원본 바이트 SHA와 대조했다. 임시 관리기 DB는 CLI 종료 시 폐기한다. 영구 서버로 바꿀 때 동일 Store/registry/ACL 경로와 별도 비밀 주입을 사용하며 운영 자동 설치는 제공하지 않는다.

격리 프로필은 이전 묶음과 동일하다: pinned Node24 Docker, network none, read-only, UID65534, cap삭제, Node permission, CPU/파일/출력/시간 제한. 실행 전 고정 confinement probe와 실제 child 안의 네트워크·호스트FS·자식실행 거절을 모두 확인한다. rootless Docker의 cgroup 하드 메모리 제한은 미지원이고 Node heap 제한은 총 메모리 제한이 아니다. Cloudflare D1 자체가 아닌 로컬 SQLite 호환 계층이며 모든 결제는 실제 소스의 simulator다.

브라우저는 [Sites SKILL.md](skill://plugin_connector_1p_689987207de08191979cf68eca2941c6/sites/SKILL.md)의 “If `cua_repl.js` is unavailable, skip browser QA” 지침 적용. 도구가 없어 preview 실행/설치/다른 조작 경로를 시도하지 않았다. `hope_browser_contract.py`는 필요한 소스·인증·데이터·뷰포트·행동과 미실시 사유를 결과에 기록한다. native details의 브라우저 실제 동작은 다음 지원 환경에서 검증한다.

키·세션·비밀번호는 산출물에 넣지 않는다. 관리기 테스트 인증값과 서비스 seed/암호화 키는 메모리에서만 사용한다. 원시 원장은 합성 테스트 자료다. HTML의 password 입력 요소와 공개 합성 로그인 암호 표기는 저장 전에 [REDACTED]로 대체하고 이 가공을 원시 증거에 명시한다. 나머지 DOM 본문은 독립 용어 대조에 사용한다. 본인 원금과 가입비·무료 보너스의 분리, BP 상한·BP/SP 순서·수수료0은 기존 고객 TEST 예제 범위이며 운영 규칙 승인으로 표현하지 않는다. 실제 결제·발송·운영자료 변경·유료 자원 생성·배포는 수행하지 않았다.
