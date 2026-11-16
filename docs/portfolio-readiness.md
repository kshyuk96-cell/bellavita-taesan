# 포트폴리오 등록·예외 보고·관측 요건 커버리지

이 문서는 이번 묶음의 신규 프로젝트 일괄 등록, 예외 묶음 보고, 준비 요건 산정 API 범위를 설명합니다. 전체 20개 항목 완료, 실제 고객 등록·운영, 상시 가동 또는 90% 품질을 주장하지 않습니다. 별도 테스터 엔진을 추가하지 않습니다.

`PortfolioReadiness(core, connections=None, budget=None, operations_reporting=None, host_admission=None)`는 동일한 ThreeTier/Store/Registry에 연결합니다. 기존 `ControlOnboarding`, 캐시된 `Connections`, `ProjectConnectionPreflight` 소스 검사, `OperatingBudget`, `HostAdmission`, `OperationsReporting`을 재사용합니다. `host_admission`을 생략하면 `budget.host_admission`을 사용합니다. 준비도 계산에 임의 성공 bool이나 점수 입력을 받지 않습니다.

## 소유자 일괄 등록

`register_batch(owner, request_key, registrations)`에 1~20개의 정확한 객체를 전달합니다.

```python
{
    "project": "new-project",
    "config": { ... },  # Registry.configuration의 정확한 필드
    "expected_revision": 0,
    "expected_fingerprint": None,
}
```

새 프로젝트는 이전 revision 0/fingerprint None이어야 합니다. 기존 프로젝트는 현재 revision/fingerprint와 설정 내용이 모두 같아야 하며 설정을 덮어쓰지 않습니다. 프로젝트 저장소 바인딩도 변경할 수 없습니다. 설정은 maintenance 비활성화, review 목적 서비스만 허용합니다. 역할·개발 수신자·자격증명·예산 정책을 생성하지 않습니다.

전체 소유자 권한·범위를 먼저 확인한 뒤 한 SQLite 트랜잭션에서 프로젝트/Registry 메타데이터와 필수 거버넌스 등록을 저장합니다. 일부 실패는 전체 롤백됩니다. 같은 소유자의 같은 요청 키는 정확히 같은 등록 범위와 현재 바인딩일 때만 재사용됩니다. 변경된 요청·등록 범위는 새 검토가 필요합니다.

**메타데이터 등록은 호스트 활성화가 아닙니다.** 기존 manager credential의 프로젝트 범위, ContinuousHost의 project map, 활성 루프 범위, native worker 연결, 모델, 예산 또는 maintenance 상태를 자동 확장하지 않습니다. 실제 신규 고객 등록은 이 개발 작업에서 실행하지 않았습니다.

## 현재 관측 요건 커버리지

`assess(token, projects)`는 1~20개 명시된 등록 프로젝트의 현재 권한과 DB 상태를 확인합니다. 다음 12개 요건을 `observed_met` 또는 `unmet_or_unknown`으로 구분합니다.

1. 현재 인증/범위에 맞는 Registry 바인딩
2. 별도로 소유자가 허용한 신규 작업 maintenance 상태
3. 설정에 맞고 300초 이내인 SourceWatch head/status
4. 같은 소스 SHA·revision에 연결된 receiver/sandbox/host 관측
5. 서로 독립적이고 현재 권한이 유효한 manager/developer/reviewer 등록
6. 전역 및 프로젝트 실행/검토 한도 등록
7. 현재 설정에 연결된 QA 정책 등록
8. 신뢰된 QA provider 등록
9. 등록된 admission 예산 정책, 인증 최신성 및 현재 한도
10. 실제 HostAdmission의 현재 로컬 호스트/격리/저장소/애플리케이션 바인딩 검사
11. 정확한 선택 프로젝트 범위를 가진 현재 continuous leader lease와 유효한 manager 등록
12. 복구 후 재조정 격리 해제 상태

호스트 gate가 없거나, QA gate/테이블이 없거나, 기존 호환 모드 예산이 정책 없이 `allowed`인 경우는 완료로 세지 않습니다. 권한 철회, 미래/오래된 관측, 소스 불일치, 한도 소진 및 설정 변경은 커버리지에서 제외됩니다. 소스 사본이나 로컬 fixture 통과를 live 연결로 바꾸지 않습니다.

결과의 `observed_requirement_coverage: {met, total}`는 이 관측 요건의 개수입니다. `percentage`는 None이고 `always_on_verified`, `uptime_measured`, `execution_authorized`, `operational_authorized`는 false입니다. 모두 충족해도 해당 시점의 선행 요건 관측일 뿐입니다. 지속 관측 구간, 실제 task owner/policy/QA/lease 승인 또는 모델·고객 품질 검증을 대신하지 않습니다. 실제 외부 observer refresh나 업무 실행은 `assess`에서 호출하지 않습니다.

## 예외만 묶은 보고

`exceptions(token, projects, request_key, after=None, limit=20)`는 기존 `OperationsReporting.report`의 묶음/커서를 재사용하고 현재 준비 요건의 미충족·미확인 항목만 프로젝트·사유별로 묶습니다. 기존 작업의 설명이나 개인별 메시지를 만들지 않습니다.

`readiness_window`와 `operations_window`는 서로 다른 제한을 명시합니다. 준비도 예외는 최대 20개 프로젝트 × 12개 요건이며 요청된 limit까지만 표시합니다. 잘리면 partial을 표시하고 전체 요약 완료로 주장하지 않습니다. 필요하면 프로젝트를 좁혀 `assess`에서 모든 요건을 확인합니다. 기존 운영 보고의 커서는 기존 운영 그룹 페이지에만 적용됩니다.

현재 준비 요건의 시각과 기록된 운영 보고의 시각을 각각 반환합니다. 운영 보고가 미설정·오래됨·일부 수집이거나 표시 범위가 잘리면 `summary_complete`는 false입니다. 보고 수집 중 callbacks가 있더라도 이후 호출자 권한과 설정을 다시 검사합니다. 알려진 bearer 값은 입력 키와 출력 전체에서 거부합니다. 메시지 전송, 개별 업무 알림, 자동 승인 또는 호스트 범위 활성화는 수행하지 않습니다.
