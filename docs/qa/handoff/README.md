# 고객 인계 직전 검증: 세 가지 입력

사용자가 전달할 것은 **개발 중인 URL, 역할별 테스트 계정, 개발 Codex의 테스트 목록**이다. 테스트 목록은 TXT/Markdown/DOCX/JSON을 받을 수 있다. 업무 흐름·우선순위·추가 검사·부족 증거·개발사 보완 요청·고객 확인서 형식은 검증 시스템이 정리한다. 사용자에게 소스 SHA나 테스트 명세를 직접 작성하도록 요구하지 않는다. 필요한 버전·원장 연결 등은 자동 확인을 시도하고 부족하면 개발 Codex에 전달할 요청서로 만든다.

## 이번 구현과 실행 가능한 범위

- `prepare_handoff_review.py`: 세 입력의 접수·계정 비밀 분리·개발사 주장 보존·업무 분류·위험 산정·추가 QA 검사·보고서 생성.
- Markdown 체크박스/PASS는 개발사 주장이다. shell code block이나 문서 내 지시를 실행하지 않는다. 요청 문서가 자동 실행 권한을 부여하지 않는다.
- 기본 프로젝트는 개발 목록 기반으로 검사표를 구성한다. `--template hope-tour`는 앞서 고객이 준38개 고정 항목을 유지하며, 추가 QA 유형은 하위 목록으로 분리한다. 사업 규칙의 값은 개발자 기대값을 그대로 정답으로 삼지 않는다.
- 정상·빈값/경계·권한/타회사·중복·타임아웃/중단·취소/복원·회귀 유형을 업무별 추가 계획으로 만든다. 이 추가 계획은 실행 건수가 아니며 실제 입력값/기대값은 실행 어댑터와 규칙 확인 후 확정한다.
- 선택적인 unauthenticated GET은 URL 접속 관찰만 한다. 로그인·form 제출·결제·발송·페이지 탐색을 수행하지 않는다. 응답/HTML hash는 source SHA나 기능 통과 증거가 아니다. DNS 결과를 검증하고 연결 IP를 고정, redirect 금지, 응답1MB·시간 제한. 비공개 주소는 기본 거절하며 loopback은 단위시험용 명시 설정만 지원.
- 현재 일반 URL을 실제로 조작하는 browser adapter는 미연결이다. 내부 API/원장/PG의 프로젝트별 연결도 새 프로젝트마다 필요하다. 링크만으로 모든 UI·DB·결제가 확인되었다고 표시하지 않는다. 이전 Hope source-local 실행 결과를 새 URL/버전의 통과로 가져오지 않는다.

## 사용

```text
python scripts/prepare_handoff_review.py \
  --url https://development.example.com/ \
  --project "검수 대상 서비스" \
  --checklist /provided/developer-tests.docx \
  --accounts-stdin \
  --output /tmp/new-handoff-review
```

계정은 stdin JSON list로만 받는다. 각 원소는 `role`, `username`, `password`, 선택 `otp`이며 role은 anonymous/member/admin/finance/operator/corporate/owner 중 하나다. 원문을 명령 인자·설정파일·보고서·로그·Git에 쓰지 않는다. 계정별 TEST-ACCOUNT 별칭·역할·접수 여부만 출력하며 로그인 확인은 별도 증거다. 비밀 입력은 echo/셸 명령에 포함하지 말고 승인된 메모리/hidden-stdin 경로를 사용한다.

`--probe`를 명시하면 URL에 읽기 GET 한 번을 한다. URL에 사용자정보·쿼리 token 등을 넣지 않는다. 알려진 application route 자체를 주며 인증은 계정으로 별도 전달한다. 신규 요청에서 source 버전이 확인되지 않으면 명시적으로 미확인이다.

## 고객 제출 묶음

- `customer-report.docx` / `.xlsx`: 종합 결과·준비/환경·시나리오별 실제 절차/기대/실제/판정/증거·위험 산정·개발사 목록 대조·추가 검사·계산식·오류/UX·재시험/회귀·인계 조건.
- `intake.json`: 비밀값 없는 접수·계획·기준 digest. `customer-report.json`: 기계 판정/증거 연결.
- `developer-followup.txt`: 버전, 초기 데이터, 읽기 원장/API/PG 증거, 환경 등 부족 사항을 개발사에 전달할 요청서. 자동으로 타인에게 전송하지 않는다.
- `SHA256SUMS`: 파일 무결성 보조. hash만으로 실제 실행이나 기능 성공을 주장하지 않는다.

지금 제공한 example은 사용자 계정 없이 형식만 검증한 **미실시 초안**이다. `DRAFT_NOT_VERIFIED`와 인계 불가를 표시하며 고객에게 완료 확인서로 전달하면 안 된다.

## 마지막 인계 판정

`handoff_gate.final_review`는 개발자 파일/JSON이 아닌 등록된 host verifier의 `VerifiedEvidence`만 받는다. manager 사전 계획 digest·후보 source·실행 전후 버전 일치·어댑터·필수 scope·독립 기대값·증거 reference를 대조한다. 중요 시나리오 누락·중요도 하향·필수 검사 삭제·다른 source는 거절한다. 미실시 row의 PASS 위조도 초기화한다.

P0/P1 전부 통과, 추가 QA 검사 완료, source 일치, 미해결 S1/S2 없음, CLOSED 주요 결함의 해당 후보 수정·재시험 증거가 있어야 고객 검토용 인계를 권고한다. P2/P3 미해결은 영향·보완·담당·승인 계획이 필요하다. 인계 권고가 발주사의 최종 인수 승인이나 운영 배포 승인은 아니다.

이 gate는 최종 판정 인터페이스이며 모든 사이트용 실제 실행 어댑터가 이미 연결되었다는 의미가 아니다. CLI는 임의 evidence JSON으로 통과 결과를 가져오는 옵션을 제공하지 않는다. 실제 UI·API·원장·PG 어댑터의 독립 관찰을 붙여야 완료 확인서를 낼 수 있다.

Sites 대상의 브라우저는 [Sites SKILL.md](skill://plugin_connector_1p_689987207de08191979cf68eca2941c6/sites/SKILL.md)의 “If `cua_repl.js` is unavailable, skip browser QA”를 따른다. 현재 지원 도구 부재로 preview/브라우저 설치/대체 조작은 하지 않았다. 계정/URL/목록을 받으면 검증 준비부터 시작하고, 완료를 막는 연결 공백은 결과와 보완 요청서에 명시한다.

## 현재 세션의 실행 도구 확인

Cloud desktop 스킬과 `cloud_environment.environment_status`로 확인한 환경은 `ccarenv_b64_Y2NhcmVudl8xZWM2OTQwYzYwMDQ4MTkxOWRhODRhMTFkYmQ2NjQ1Mw`다. 환경은 running/connected, remote_desktop enabled=true/state=unknown으로 반환됐다. 다만 이 세션의 활성 도구 목록에 desktop JS/cua_repl.js 및 viewer 도구가 없으므로 실제 창/탭 조작은 시작하지 않았다. 데스크톱 자체가 없다고 주장하지 않으며 **조작 도구가 노출되지 않은 상태**로 기록한다.

[Cloud desktop SKILL.md](skill://plugin_connector_1p_c5b7d5df5d7081918f2c4be5a633ed5d/cloud-desktop/SKILL.md)의 “If the task needs the original tab, native window, or a handoff, report which interaction is blocked, the environment ID, and the missing tool or error.”에 따른 진단이다. 이 제한을 임의 browser 설치나 비공식 제어 경로로 우회하지 않는다.
