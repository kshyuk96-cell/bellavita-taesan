# 태산이 프로젝트 실행·업무 어댑터 10건 검토

기준: PR61 `7f493a2e52cbd7613fd38460f30d744fa645a733`의 후속. 네 고객 소스는 `../connection-evidence/source-baselines.json`의 고정 SHA를 참조한다. 고객 소스나 운영 서비스는 수정하지 않았다.

| 번호 | 개발 범위 | 이번 구현과 검증 | 실제 연결 상태 |
|---|---|---|---|
| 1 | 프로젝트 작업 수신기 | 기존 AgentDirectory/worker 인증을 사용하는 `ProjectReceiver`, CLI와 등록 connector | 기존 Codex 창은 미연결, 수신기 운영 설치 안 함 |
| 2 | 격리 자동 테스트 | 네트워크·PID 네임스페이스, 고정 명령, 전용 런타임, 정확한 Git 사본, 제한 시간 | 현재 호스트는 Bubblewrap 권한 제한으로 불가. 우회하지 않음 |
| 3 | 중단 대조·복구 | claim/ack 중단, 취소/철회, 임대 만료 상태를 `recovery`로 대조 | 자동 재실행·잠금 해제 없음, 실제 호스트 복구 검수 대기 |
| 4 | 다른 에이전트의 소스 변경 감지 | 현재 소스 관측기, 내구성 변경 이벤트·중복 방지·묶음 조회·확인 처리 | 원격 관측기는 호스트가 등록해야 함; 로컬 고정 사본을 원격 최신으로 간주하지 않음 |
| 5 | 대표 위임 연결 | 기존 정확한 경로·기간·작업 수 정책에 묶음 원자 승인/현황 API 추가 | 4개 고객의 maintenance_enabled는 모두 false 유지 |
| 6 | ERP 업무 조회 | 실제 orders API 계약, 행 번호와 불변 ID, 금액 null/0 구분 | 고객 직원/서비스 인증 공급자 미등록 |
| 7 | 쇼핑몰 주문·취소 | 실제 주문 상태 조회 + 이벤트 중복·역순·취소 뒤 재승인 의심 대조 | 로컬 검토 기록만 갱신; 실제 이벤트 revision 공급자·결제 연계 대기 |
| 8 | 희망일출 상품·문의 | 실제 bootstrap products/requests, 상담 단계 호환 | 실제 읽기 공급자/직원 인증 대기 |
| 9 | 협회 권한·부고 | 현재 세션·정확한 부고 권한 증거, 최소 정보 응답 | 실제 원본 authorize 공급자 연결 대기 |
| 10 | 실패 수집·90점 회귀 | 불투명 실패 영수증·검토된 고정 회귀·ModelLab 관측 연결 | 실제 직원 실패 수집/배포 모델 실측은 미연결 |

## 확인한 흐름

`scripts/rehearse_project_runtime.py`는 실제 네 로컬 소스의 커밋/깨끗한 상태만 읽고, 별도 임시 DB와 **모의 제공자**를 사용해 루프백 HTTP에서 여섯 업무 조회, 회사 접근 차단, 이벤트 대조, 소스 변경 알림 확인, 미측정 점수 보존을 재현한다. 실행 결과는 `synthetic-loopback.json`이다. 이 결과는 실제 고객 API, 보안 격리 성공 또는 운영 인수 시험이 아니다.

```sh
python scripts/rehearse_project_runtime.py --sources DEDICATED_FOUR_SOURCE_SNAPSHOTS --output PRIVATE_REVIEW_OUTPUT
```

수신기는 `kind=test` 작업만 처리한다. 기존 네이티브 개발 인계 기능은 그대로 두며 지시문을 쉘 명령으로 실행하지 않는다. 격리 엔진이 준비되지 않으면 claim 전에 멈춘다. 별도 관리 코드의 고정 커밋에서 권한/승인 회귀를 실행하며 일반 업무 테스트를 권한 시험으로 바꿔 부르지 않는다. 실제 Node/V8 자원 제한과 격리 빌드 검증도 남아 있다.

## API

기존 `/v1/representative/` 인증·JSON 크기·CSRF 경로 아래에 `/runtime/`이 추가됐다. 서버가 `project_business_providers`, `project_source_probes`, `project_failure_profiles`, `project_receivers`를 등록한다. 요청에서 이 콜백이나 실행 코드를 선택할 수 없다.

| 메서드·하위 경로 | 동작 |
|---|---|
| POST business/read | project/action/선택 query로 등록 업무 조회 |
| POST business/event | project/order_id/event_key로 원본 이벤트 조회 후 로컬 검토 |
| POST source/inspect, source/pending, source/ack | 관측·프로젝트 묶음 변경 조회·확인 |
| POST delegations/activate, delegations/inventory | 정확한 위임 ID/지문 묶음 승인·범위별 잔여 예산 조회 |
| GET failures?project=... | 검토용 실패 영수증 조회 |
| POST failures/collect, failures/review, failures/evaluate | 고정 실패 수집·영역별 회귀 선택·실제 등록 관측 채점 |
| POST receiver/check, receiver/recovery | 등록 작업자만 자신의 수신기 점검·중단 대조 |

HTTP로 수신기 실행을 시작하는 `receiver/run`은 제공하지 않는다. 기본 CLI는 점검만 하고 `--run`을 명시한 기존 비공개 작업자 프로필에서만 대기열을 소비한다. 미연결/권한 부족/조회 실패/불명확/실제 빈 결과를 구별한다.

## 형님께 필요한 연결을 묶어서 검토

1. 전용 관리·실행 호스트의 비용/영구 저장소/원점을 기존 배포 계획과 함께 확정하고, 실제 격리와 재시작 보존을 시험한다. 기존 운영 Render 한 곳을 개발 실행기로 사용하지 않는다.
2. PC에서 기존 개발 연결 인증을 완료한 뒤 프로젝트별 작업자·인증 공급자를 등록한다. 기존 Codex 창은 소스 접근만으로 원격 제어되지 않는다. API key나 token을 채팅으로 받지 않는다.
3. 각 프로젝트의 서비스 인증·고정 소스·실행 경로·필수 검사·기간·작업 수를 하나의 설정 검토안으로 확인하고 활성화한다. 개발 승인과 실제 운영 적용 조건을 유지한다.

호스트 생성, 요금 변경, 상시 수집/예약, 고객 소스 쓰기, 실제 결제·환불·발송·운영 시트 변경은 수행하지 않았다. 복구는 새 관리 수신기를 멈추고 상태를 보존해 대조하는 것이며, 외부 효과의 자동 취소가 아니다.

## 점수

이번 합성 통합 시험과 코드 회귀 통과를 운영 AI 점수에 합산하지 않는다. 직전 PR61의 네이티브 개발 에이전트 텍스트 실험실 100/100도 직원 실사용 성적과 다르다. 이번에 모델 API/음성/사진/카톡을 새로 실측하지 않았으며, 운영 이해 62.5·판단 62.5·개발 75를 유지한다.

이번 후보 전체 Python 회귀는 **843개 통과**했다. 신규 위임 묶음·소스 감지·업무 어댑터·실패 회귀·통합 API·수신기 관련 **120개**가 포함된다. 테스트 실행 중 운영 자격증명을 주입하지 않았으며 `git diff --check`도 통과했다.
