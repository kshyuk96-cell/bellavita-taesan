# 승인된 업무 기준의 작업자·검토자 인수

`WorkerQualityHandoff(core).generate(token, task_id, role, candidate_digest=None)`는 인증된 소유자/관리자가 실제 task에 고정된 업무 기준을 양쪽 역할에 전달하도록 준비합니다. `role`은 developer 또는 reviewer이며, reviewer에는 현재 frozen candidate의 정확한 digest가 필요합니다. 임의 PASS, 점수, 성공 bool 또는 worker lease 입력은 받지 않습니다. 외부 callback, 모델 실행, 원격 fetch, ArtifactContent 본문 읽기나 새 테스터 엔진은 없습니다.

## 같은 승인 기준을 양쪽에 전달

`BusinessBaselines`의 승인된 계약은 디자인, 문구, 가격 동작, 완료 기준, 통합 요구사항을 manager-owned QA 시나리오 ID에 연결합니다. ProposalBoard의 불변 snapshot과 소유자 task approval의 `business_baseline` pin이 현재 계약 version/digest/revision/config/QA policy와 정확히 같아야 합니다. 보관 자료의 이름이나 자유 문구만으로 승인 권한을 만들지 않습니다.

패킷에는 다음이 함께 들어갑니다.

- 정확한 task ID/version/manifest/config 및 owner-approved path/dependency policy
- 승인된 requirements와 preserve 조건
- 승인된 reference ID/SHA256/설명
- 현재 QA 정책에서 모든 요구사항의 mapped scenario를 빠짐없이 선택한 independently stored `expected_contract` projection, required coverage, 원래 전체 contract/policy digest
- 역할과 현재 등록 recipient, source pin 및 reviewer candidate identity

개발자와 검토자 패킷의 승인 기준 부분은 같은 `criteria_digest`를 가집니다. `binding_digest`는 task/baseline/QA/role/recipient/source/candidate를 함께 묶는 무결성 hash이며 **서명이나 실행 권한이 아닙니다**. 역할과 candidate가 다르면 이 hash도 달라집니다. 모든 결과는 `execution_authorized`, `technical_acceptance`, `operational_authorized`, `quality_equivalence_guaranteed`가 false입니다.

기준이 없는 기존 프로젝트는 `state: not_bound`로 명시됩니다. 기준을 설치한 프로젝트에서 pin이 없거나 달라진 기존 task는 성공으로 취급하지 않습니다. 현재 인증, 설정, task/정책 pin, QA policy, SourceWatch 최신성, reference hash/유효기간 또는 frozen candidate identity가 달라지면 전달이 차단됩니다. 한 DB transaction에서 검증하며 반환 직전에 호출 권한과 기준을 다시 확인합니다.

## 참조 전달과 실제 시각 검증의 경계

참조는 본문이 아닌 메타데이터입니다. `materialized`, `body_transferred`, `reference_materialized`는 false이고, 참조가 있으면 `reference_delivery_pending`이 true입니다. `visual_validation`은 `not_measured`입니다. 작업자가 이미 이미지를 보았거나 디자인을 검증했다고 주장하지 않습니다. 본문을 system instruction으로 자동 승격하지 않습니다.

실제 시각 자료를 양쪽에게 보여 주려면 별도의 신뢰된 host 경로가 기존 ArtifactContent의 권한·hash·만료 검사를 거쳐 읽고, 승인된 task에만 연결된 read-only 참조 묶음을 전달해야 합니다. 이 모듈은 그 전송을 수행하지 않습니다. 현재 기준이 QA 시나리오에 연결되어 있어도 참조 렌더링/디자인 검수 완료를 뜻하지 않습니다.

## 실제 prompt 연결

Root-owned integration은 신뢰된 `quality_provider` 결과를 developer의 claimed assignment와 reviewer의 frozen candidate에 `quality_handoff`로 넣어 기존 native prompt에 추가합니다. 이 값은 승인된 scope constraints의 데이터 문맥이며 도구 권한을 확대하는 명령이 아닙니다. 기존 current task/worker claim/live lease/independent reviewer/actual source/QA acceptance fence를 그대로 통과해야 합니다. Reviewer packet의 `candidate_live_recheck_required`도 true이며 DB 메타데이터만으로 실제 frozen checkout 재검사를 대신하지 않습니다.

이 인수 방식은 작업자가 바뀌어도 같은 승인 기준을 보존하기 위한 것입니다. 서로 다른 모델이 동일한 품질이나 성능을 낸다고 보장하지 않습니다. 실제 구현·독립 검토·QA 및 필요한 별도 디자인 검수 결과를 계속 확인해야 합니다.

## Native prompt 크기와 QA projection

전체 QA 정책의 `contract_digest`는 그대로 검증하고 `qa_contract.digest`에 유지합니다. 실제 전달하는 `expected_contract`는 `qa_projection(contract, policy)`로 승인된 모든 요구사항의 `scenario_ids` 합집합만 선택합니다. 무관한 시나리오만 제외하며 요구 시나리오를 하나라도 누락하면 실패합니다. `qa_contract.projection_digest`, `is_full`, `mapped_ids`로 전달 범위를 명시합니다. Projection은 QA 판정 결과가 아니고, 전체 정책의 실제 검증 범위를 줄이지 않습니다.

BusinessBaselines의 승인 경로는 계약 전체 12KB 및 mapped QA를 합친 16KB context budget을 확인합니다. 작업자·검토자에는 동일 projection과 `criteria_digest`가 전달됩니다. 독립 fixture에서는 전체 정책에 무관한 1,000개 행을 추가해도 승인된 scenario 하나가 유지되고 전체 정책 해시는 유지되는 것을 검증했습니다. Native transport는 바깥 task/policy 문맥을 포함한 실제 전체 prompt 한도를 별도로 확인해야 합니다. 필수 기준을 조용히 잘라내어 크기를 맞추지 않습니다.
