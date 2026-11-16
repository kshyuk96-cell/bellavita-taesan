# 중앙 거버넌스 서비스 실행

이 구성은 기존 `ContinuousHost.load_host`의 저장소·개발자·리뷰어·Fleet·전역 실행 제한을 그대로 사용하면서 인증 HTTP와 관리자 루프를 한 프로세스에서 실행합니다. 실제 클라우드 설치, 유료 호스트 생성, 배포 또는 24시간 가동 보장은 수행하지 않았습니다.

소유자만 읽고 쓸 수 있는 실제 파일(예: 권한 0600)에 다음 설정을 저장합니다. 경로는 해당 호스트의 기존 비공개 설정 파일로 바꿉니다.

```json
{
  "version": 1,
  "host_config_file": "/var/lib/taesan/host.json",
  "listen": {"host": "127.0.0.1", "port": 8791},
  "governance": {}
}
```

`host.json`은 기존 연속 관리자 설정입니다. 실행 애플리케이션의 깨끗한 커밋, 프로젝트 경로, 분리된 Codex 프로필과 역할별 토큰 파일을 검증합니다. HTTP 요청이나 위 JSON에 실행 명령, Python 모듈 경로, 콜백, QA 통과 여부를 넣을 수 없습니다. 외부 인터페이스에는 HTTPS 역방향 프록시의 명시적인 `public_origin`이 필요하며 기본값은 루프백입니다. 기존 HTTP 인증 및 CSRF 검증을 사용합니다.

```bash
python scripts/run_governance_service.py --config /var/lib/taesan/governance.json --status
python scripts/run_governance_service.py --config /var/lib/taesan/governance.json --once
python scripts/run_governance_service.py --config /var/lib/taesan/governance.json --max-ticks 3
python scripts/run_governance_service.py --config /var/lib/taesan/governance.json
```

소스 및 QA 연결은 신뢰된 서버 코드가 `build_service(..., source_probes=..., qa_providers=..., governance=True)`로 등록합니다. JSON 실행기는 임의 연결을 불러오지 않으므로 별도 연결 등록이 없으면 소스는 `not_configured`이고 자동 실행이 차단됩니다. 필수 QA 정책이 있는 프로젝트는 등록된 QA 제공자가 없으면 승인되지 않습니다. 합성 QA 증거는 실제 프로젝트 증거로 승격되지 않습니다.

거버넌스 모드의 각 관리자 주기와 HTTP tick/reconcile은 선택한 프로젝트의 읽기 전용 소스 관찰을 먼저 갱신합니다. 기존 `poll_seconds`는 1~60초이며 제안의 소스 관찰 유효기간은 300초입니다. `baseline`, `unchanged`, `changed`만 다음 실행 판단으로 진행합니다. 누락·권한 거절·연결 실패·오래된 응답은 실행을 막고 최신 상태를 기록합니다. 마지막 성공 소스는 보존하지만 최신 실패가 있는 동안 제안 승인과 실행에 사용할 수 없습니다. 소스 변화는 자동 업무 승인이나 재실행을 뜻하지 않습니다. 제공자는 자체 제한 시간 안에 응답해야 합니다.

API 접두사는 `/v1/representative/runtime/governance/`입니다. 인증된 `POST proposals/create`, `POST proposals/list`, `GET proposals/get?proposal_id=...`, `POST batches/create`, `GET batches/get?batch_id=...`, 소유자 전용 `POST batches/approve`를 사용합니다. 배치의 정확한 digest를 승인해도 기술 승인이나 네이티브 전달 권한이 생기지 않습니다.

SIGTERM/SIGINT는 새로운 관리자 실행을 차단하고 HTTP 수신을 종료합니다. 진행 중인 외부 작업이나 오래된 불명확 작업이 종료되었다고 판단하지 않으며 불확실한 작업은 자동 재전송하지 않습니다. 프로세스 감독 예제는 `ops/taesan-governance.service.example`이며 설치하거나 활성화하지 않았습니다.
