# 승인 기준·미완료·검토 누락 한 묶음 보고

`QualityDeliveryReport(core, baselines)`는 동일한 core/store의 `BusinessBaselines`와 기존 source/QA/역할/task 기록을 읽어 소유자 또는 범위가 있는 관리자에게 한 번에 보고합니다. 테스터 엔진이나 대량 테스트를 만들거나 실행하지 않습니다. 점수, 완료 백분율, 작업자 간 동일 품질 보장, 최종 사업 승인 또는 운영 배포 승인을 만들지 않습니다.

```python
reporter = QualityDeliveryReport(core, baselines)
report = reporter.report(manager_token, ['registered-project'],
                         limit=20, cursor=None, freshness_limit=3)
```

`baselines=None`이면 기준을 임의로 만들어 넣지 않고 `not_configured`로 표시합니다. 요청자의 인증·역할·프로젝트 권한 오류를 기준 미구성이나 미측정으로 숨기지 않습니다. 조회 전후와 최종 트랜잭션에서 설정 fingerprint와 baseline revision/digest를 확인합니다.

## 승인된 기준을 정확히 표시

프로젝트별 기준 상태(`approved`/`stale`/`not_configured`), 버전·digest·revision·설정·QA 정책 pin을 표시합니다. 승인된 요구 항목의 ID, 카테고리, 우선순위, criterion과 기존 QA scenario 매핑을 보여 줍니다. preserve 제약과 승인된 reference의 불투명 ID·SHA·설명을 함께 표시합니다. 이 텍스트는 소유자가 고정한 데이터이며 실행 지시나 콜백으로 해석하지 않습니다.

reference는 현재 메타데이터 참조이며 이미지·문서 본문을 작업자 모델에 실제 전달했다고 판단하지 않습니다. 각 참조는 `reference_materialization_pending: true`, `body_delivered_to_workers: false`입니다. 최상위 `reference_bodies_delivered_to_workers`도 false입니다. 승인 기준에 이미지 SHA가 있다는 사실을 디자인 비교나 이미지 이해 검증 완료로 계산하지 않습니다.

요구 항목마다 `business_delivery_verified`는 false이고 `evidence_state`는 `not_independently_verified`입니다. 현재 시스템의 synthetic QA나 기계적 검사 성공은 해당 사업 기준의 실제 충족 또는 최종 사업 승인이 아닙니다.

## 미완료와 검토 누락을 구분

입장한 업무는 기존 `core.overview`를 한 번 읽습니다. 기본 전역 20건이며 최신성 검사는 기본 최대 세 건입니다. 보고는 다음 사실을 구분합니다.

- owner 기준 pin이 현재 승인 버전·revision·설정·QA 정책과 일치하는지
- 개발·리뷰가 아직 끝나지 않았는지, 검토가 완료되었다는 기록인데 독립 reviewer/check/code assessment 증거가 없는지
- 기술 승인 이력과 현재 소스/역할/QA 최신성이 확인된 기술 승인을 구분
- QA 정책과 제공자 존재 여부, 합성 QA와 실제 business QA의 차이
- 활성 taesan/developer/reviewer 등록 상태와 오래되거나 해제된 역할 연결

보고는 검사 이름·exit code, candidate digest와 실제 활성 독립 reviewer 바인딩도 확인합니다. 기존 검토 증거가 없거나 바인딩이 다르면 core의 과거 `accepted` 상태만으로 현재 기술 승인이라고 출력하지 않습니다. callbacks 동안 기준이 바뀌면 현재성 판단을 무효화하고 변경 사실을 남깁니다. QA receipt가 최종 보고 시점에 오래되었다면 이전 관찰의 최신성을 유지했다고 추측하지 않습니다.

`bridge_tasks`를 별도로 집계하므로 아직 hierarchy에 배정되지 않은 승인 대기·queued 업무도 숫자에서 빠지지 않습니다. `unfinished_recorded`는 **기록된 workflow 상태**의 미완료 수이며 사업 결과의 미완료 전체 건수나 품질 점수가 아닙니다. 취소 이력은 state counts에 남고, 현재 기준 없는 과거 완료가 새 기준을 충족한 것으로 바뀌지 않습니다. 보고는 task approval·QA 증거·baseline pin을 수정하지 않습니다.

## 창과 권한 범위

업무 커서는 기존 overview의 요청자·프로젝트·설정 바인딩과 수명을 사용합니다. 프로젝트별 기준은 최대 20개 요구와 10개 참조를 표시하고, 역할 확인은 최대 200개 등록을 검사합니다. 기준 요구가 더 많거나 업무/역할 표시 창이 잘리면 `partial`과 `summary_complete: false`를 표시합니다. `summary_complete`는 수집 창의 범위 표시이며 사업 완료나 품질 승인 플래그가 아닙니다.

기준·QA 정책·역할·source watcher 상태는 source/QA 콜백 이후 DB에서 다시 읽습니다. 역할·설정·baseline 소유자 권한이 바뀐 값을 오래된 callback 결과로 덮어쓰지 않습니다. 보고는 task 설명·원시 로그·프롬프트·자격 증명·비공개 호스트 경로를 반환하지 않습니다. 실제 고객 외부 시스템이나 작업자 모델을 추가로 호출하지 않고 기존에 등록된 overview 최신성 경계만 사용합니다.

항상 `quality_score: null`, `worker_quality_equivalence_verified: false`, `business_acceptance_verified: false`, `operational_authorization: false`, `tester_engine_started: false`입니다. `messages_sent`도 false이며 업무별 외부 알림을 보내지 않습니다.
