# 운영 결과물 메타데이터와 묶음 보고

`OperationsReporting(core, control_digest, controller=None, durability=None, artifact_sources=None)`는 동일한 core/store에 등록된 서비스만 결합합니다. 외부 메시지, 네트워크 요청, 업무 실행, Tester 엔진 또는 운영 배포를 추가하지 않습니다. 보고는 등록된 `ControlDigest`를 한 번 읽고 연속 관리자 상태와 로컬 인증 알림을 조회합니다. 인증 알림 조회에는 기본 `GovernanceDurability(core)`를 사용할 수 있습니다. 소스/QA 최신성 콜백은 기존 digest의 제한을 따르며 보고 페이지를 넘길 때 반복 실행하지 않습니다.

## 결과물의 실제 관찰과 참조

결과물 제공자는 신뢰된 서버 코드에서 `(project, kind) -> observer(project, artifact_key)`로 등록합니다. 허용 종류는 `candidate`, `review_evidence`, `qa_report`, `backup_manifest`, `diagnostic`입니다. 등록된 관찰자는 `ArtifactObservation(project, kind, artifact_key, data, observed_at)`를 반환합니다. `data`는 실제 읽은 최대 8 MiB의 bytes이며 서버가 SHA-256과 크기를 계산합니다. 관찰 시각은 유한하고 현재 기준 300초 이내여야 합니다. 사용자가 전달한 hash, 크기 또는 업로드 성공 여부를 증거로 받아들이는 API는 없습니다.

```python
from manager_bridge.operations_reporting import OperationsReporting, ArtifactObservation

# 이 함수는 이미 허용된 서버측 결과물 저장소의 관찰자입니다.
# artifact_key는 불투명 식별자이며 이 모듈은 경로로 해석하지 않습니다.
def observe_registered_qa(project, artifact_key):
    data, observed_at = trusted_result_store.read_registered(project, artifact_key)
    return ArtifactObservation(project, 'qa_report', artifact_key, data, observed_at)

reports = OperationsReporting(core, control_digest, controller=controller,
    artifact_sources={('registered-project', 'qa_report'): observe_registered_qa})
receipt = reports.register_artifact(manager_token, 'registered-project',
    'qa_report', 'observation-001', retention_seconds=86400)
metadata = reports.get_artifact(manager_token, receipt['reference'])
```

이는 신뢰된 호스트 조립 예시이며 `trusted_result_store`는 자동 생성하거나 발견하지 않습니다. 제공자가 없으면 등록 요청은 `NotFound`로 차단됩니다. HTTP가 bytes, 파일 경로, callback 또는 모듈을 등록하지 않습니다. 이 모듈은 임의의 호스트 경로를 읽거나 hash하지 않습니다.

저장하는 표준 메타데이터는 불투명 참조, 프로젝트·설정 바인딩, 종류, SHA-256, 실제 byte 수, 관찰 시각과 논리적 만료 시각입니다. 원시 bytes, 로그, 프롬프트, 프로필 경로 또는 비밀 값을 저장하지 않습니다. 참조 조회의 `retrieval`은 항상 `metadata_only`입니다. 실제 결과물 내용의 다운로드나 원본 보관은 별도 제공자의 책임입니다. SHA는 결과물 바이트의 동일성을 나타낼 뿐 QA 통과·기술 승인·업무 승인·운영 반영 권한을 주지 않습니다.

보관 기간은 60초~30일이고 기본은 1일입니다. 같은 프로젝트·설정·종류·artifact_key는 불변이며 bytes가 바뀌면 새 키가 필요합니다. 논리적으로 만료된 메타데이터는 `expired`로 표시하지만 원본이나 메타데이터 파일이 실제 삭제되었다고 주장하지 않습니다. `physical_deletion_verified`와 `acceptance_authorized`는 항상 false입니다. 프로젝트 설정이 바뀌면 예전 참조를 현재 증거로 조회하지 못합니다.

## 긴급/일반 묶음 보고

`report(token, projects, request_key, after=None, limit=20)`은 명시한 최대 100개 프로젝트의 인증과 설정을 확인합니다. `limit`은 1~100입니다. digest/status/알림 조회 뒤와 스냅샷 저장·반환 직전에 권한 및 설정 바인딩을 다시 확인합니다. 각 결과물 관찰도 콜백 전후에 같은 검증을 수행합니다.

- **긴급**: 실제 기록된 `uncertain` 실행과 `credential_refresh_required` 알림을 프로젝트·종류별로 묶습니다. 자격 증명 주의 알림이 곧 만료인지 이미 해제인지 추측하지 않으며 자동 갱신·중지·재실행하지 않습니다.
- **일반**: 개발 차단, 관찰 미확인, 소유자 결정 대기, 인증 관찰 누락을 집계합니다. 항목별 메시지나 업무별 알림을 보내지 않습니다.

하나의 요청 키는 요청자 인증 식별자, 프로젝트 집합과 설정에 묶인 **기록 시점의 스냅샷**입니다. 같은 키의 재조회는 동일한 보고 ID를 반환하며 외부 관찰을 다시 하지 않습니다. 새로운 관찰은 새 요청 키를 사용합니다. `observed_at`은 기록 시각이고 300초가 지나면 `snapshot_state: stale`입니다. 기록된 완료 수는 그 시각에 관찰한 사실이며 현재 실행 허가로 사용할 수 없습니다. `acceptance_authorized`는 false입니다.

페이지 커서 `after`는 서버에 저장한 불투명 식별자로 요청자, 요청 키, 프로젝트·설정, 보고 ID와 위치에 바인딩되며 300초 뒤 만료됩니다. 권한·설정 변경이나 다른 요청자의 커서를 재사용하면 거절합니다. 다음 페이지는 같은 저장 스냅샷에서 읽습니다. 보고 스냅샷과 메타데이터를 자동 삭제하지 않으며 커서는 제한된 개수와 수명으로 관리합니다.

`group_window`는 현재 긴급/일반 묶음의 제한·전체 묶음 수·표시 수·다음 커서를 제공합니다. `input_windows`는 digest 업무/배치/소유자 결정, 연속 관리자 100건, 인증 알림 100건의 범위를 별도로 표시합니다. 표시할 다음 묶음이 있거나 원본 창이 잘렸다면 전체 완료로 읽지 않습니다. 컨트롤러가 없거나 원본 digest 범위가 불완전하면 `summary_complete`도 false입니다. 차단과 미확인을 구분하고 임의의 완료 백분율을 만들지 않습니다. 오래된 소유자 배치의 별도 페이지 처리는 상위 제어 API의 책임입니다.
