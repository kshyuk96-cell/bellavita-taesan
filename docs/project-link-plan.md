# 프로젝트 권한·역할·호스트 연결 후보 계획

`ProjectLinkPlan(core, host=None, brokers=(), templates=None)`은 기존 ThreeTier, 선택된 ContinuousHost, 등록된 SubscriptionBroker와 신뢰된 Python 설정의 호스트 템플릿을 비교합니다. 이 기능은 권한·개발자/검토자 연결·실행 설정 후보와 충돌만 검토합니다. 실제 고객 등록, 자격증명 확대, 유료 자원, 모델, 별도 테스터 또는 운영 활성화를 수행하지 않습니다.

## 호출 API

- `inventory(owner, projects)`: 1~20개 정확한 등록 프로젝트의 현재 revision/fingerprint, 역할 등록, broker reference/generation/binding, source pin, host map digest 및 진행 중 작업 차단을 반환합니다. 소유자 권한과 프로젝트 범위가 필요합니다. 공유 broker를 참조하려면 그 broker의 모든 프로젝트에 대한 소유자 범위도 있어야 합니다.
- `plan(owner, request_key, projects, expected_bindings, role_mappings, expected_host_map_digest, broker_refs)`: inventory에서 받은 정확한 Registry/host/broker 비교값과 역할 ID를 검증하고, 원자적으로 불변 후보 계획을 기록합니다. `role_mappings[project]`는 정확히 `taesan`, `developer`, `reviewer`의 기존 member ID를 지정합니다. 새 member나 토큰을 생성하지 않습니다.
- `get_plan(owner, plan_id)`: 같은 소유자의 현재 권한과 Registry·역할·broker·host map·소스·작업 상태를 다시 검사합니다. 달라진 계획은 `invalidated`이며 `current_blocks`와 현재 후보 생성 가능 여부를 반환합니다. 이전 후보는 실행 가능한 설정이 아닙니다.

같은 소유자의 같은 request key는 같은 입력과 같은 현재 관측에만 재사용됩니다. Registry revision/fingerprint, broker generation/binding 또는 host map 비교값이 달라지면 새 검토가 필요합니다. 변경된 역할 자격증명은 원문이나 해시를 노출하는 대신 내부 authority digest를 바꾸어 계획을 무효화합니다.

## 신뢰된 템플릿과 생성 결과

`templates`는 프로젝트별 기존 `continuous_host.load_host` project entry 형식의 고정 설정입니다. 공개 요청은 경로나 명령을 지정하지 않고 해당 템플릿과 등록 ID만 참조합니다. 템플릿에 허용된 필드 이외의 auth/secret 원문 필드는 거부합니다. token-file과 Codex home은 참조이며, 토큰 파일이나 broker의 `auth.json` 내용을 읽지 않습니다. 입력 템플릿과 전체 출력의 알려진 bearer 값도 거부합니다.

후보의 `generated_host_settings`는 템플릿 digest, configuration reference, 정확한 역할·broker·source references, commandset/resource digest만 포함합니다. 실제 경로·명령 argv·credential hash·인증 원문을 공개 계획에 저장하지 않습니다. 원래 private loader entry는 신뢰된 Python 템플릿에만 남습니다. 별도 신뢰된 private materialization 및 기존 source-origin/private-loader 검증이 필요하며, 이 모듈에는 apply 또는 파일 쓰기 기능이 없습니다. DB에 후보 계획 메타데이터만 기록합니다.

설정 후보에는 저장소/역할 일치, 독립된 역할 subject, 등록된 broker 대상 profile과 템플릿 home 일치, 기존 host session 또는 현재 승인된 Fleet template이 필요합니다. 다른 프로젝트의 기존 host와 다른 후보 템플릿의 source/runtime/candidate/profile이 충돌하거나 private DB·token references·subscription profile 영역과 겹치면 차단합니다. SourceWatch의 현재 설정과 최신성도 확인합니다. 이것이 실제 연결이나 구독 인증 파일 최신성 검증을 의미하지는 않습니다.

## 변경 차단과 유지되는 경계

현재 pending/queued/running/reported/needs-verification task, live lease, 미완료 managed task, running/uncertain continuous 또는 native host 작업, Fleet 생성·실행, 활성/불확실 budget reservation, 복구 재조정 격리, ready가 아닌 broker는 후보 설정 생성을 차단합니다. 차단된 항목의 `generated_host_settings`는 None입니다. 실행 상태를 지우거나 자동 복구·재시도하지 않습니다.

새 프로젝트 권한 계획은 manager credential의 프로젝트 범위나 ContinuousHost project map을 늘리지 않습니다. 역할 배정, SubscriptionEnrollment, native 세션 등록 및 실제 host 설정 적용은 각각 기존 owner 승인 흐름과 현재 실행 fence를 통과해야 합니다. 이 모듈은 SubscriptionEnrollment.apply, broker refresh/distribution, credential copy, 모델 요청 또는 실제 host 재구성을 호출하지 않습니다. 모든 결과는 candidate-only이며 `execution_authorized`, `application_performed`, `activation_performed`, `credentials_expanded`가 false입니다.
