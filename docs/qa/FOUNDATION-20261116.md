# QA 연결 기반 개발 묶음

PR #73의 접수·인수 게이트에 필요한 실행 기반을 별도 브랜치로 묶었다. 승인된 호스트 등록부, 격리 자식 실행, 단일 실행 작업자, 실행 영수증, 불확실 상태 대조, 운영자 CLI를 제공한다. 실제 스테이징 주소·계정·데이터·실제 계산 정책은 만들거나 임의 승인하지 않는다.

## 제공하는 구성

| 구성 | 역할 |
|---|---|
| `QAProfile / QARegistry` | 프로젝트·릴리스·후보 SHA·고정 suite·소스/런타임 경로·Python 실행 파일 해시·시드·한도를 등록하고 digest로 고정 |
| `QASandbox` | 기존 `BubblewrapTests`의 전체 namespace 격리를 재사용; Git의 고정 커밋만 추출하고 소스/런타임을 읽기 전용 마운트 |
| `qa_isolated_child.py` | `order-ledger-v1`과 `universal-v1`만 허용하는 고정 실행 진입점; 작업에서 shell·명령·모듈·경로를 받지 않음 |
| `QAWorker / QAClient` | 상태 확인→등록/격리 검사→한 번의 실행권→관찰→결과 제출→회수; 자동 재시도 없음 |
| 실행 영수증 | 프로필 digest·소스·네트워크 차단·프로세스 종료·관찰 보고서 digest를 결과에 결합; 필수 프로필 승인에서는 누락/불일치 차단 |
| 불확실 상태 처리 | 실행 중단·작업자 재시작·결과 전송 불확실 상태를 `reconciliation_required`로 보존 |
| 소유자 대조 | 중단과 외부 효과 없음을 독립 확인한 소유자만 증거 참조를 남겨 `reconciled`로 종료; 통과로 전환하거나 같은 작업을 재실행하지 않음 |

## 실행 경계와 한도

- 기본 실행기는 `/usr/bin/bwrap`의 network/PID 등 namespace 격리가 실제로 성공해야 실행한다. 사용할 수 없으면 `sandbox_unavailable`, 작업은 `queued`를 유지한다. 호스트 Python이나 audit hook로 자동 우회하지 않는다.
- 자식에게 호스트 환경변수·관리기 인증 파일·원본 `.git`·사용자 홈·호스트 작업 공간을 마운트하지 않는다. 별도 승인된 Python 런타임과 커밋 archive만 제공한다.
- 작업자가 제공한 입력을 명령으로 실행하지 않는다. 어댑터 추가는 suite 코드·호스트 프로필·사전 계약의 별도 검토 후 수행한다.
- 실행 시간 1~3,600초, 관찰 보고서 최대 32 MiB, 자식 주소 공간 1.5 GiB, 파일 크기 최대 256 MiB, 파일 디스크립터 256, 프로세스 수 128의 제한을 적용한다. 실행 중 출력도 감시한다. 프로세스 수 제한은 OS 사용자별 제한이며 cgroup 자원 격리를 대신하지 않는다.
- 관리기 heartbeat는 별도 감시 스레드로 처리한다. HTTP 확인이 지연돼도 주 실행 감시가 시간 제한을 집행하고 프로세스 그룹에 TERM/KILL을 보낸다. 자식 프로세스가 이미 종료한 경우에도 남은 그룹을 정리한다.
- 원본 stdout은 보고서로만 파싱한다. stderr와 자식 오류 메시지를 보고서/로그에 복사하지 않는다. 종료 코드 0이어도 보고서·계약·단언·증거가 없으면 통과하지 않는다.
- 실행 영수증은 인증된 신뢰 작업자의 관찰이다. 해시나 `isolation_verified` 필드만으로 악의적 작업자의 독립성을 증명하지 않는다. 작업자 토큰은 이 승인 실행기에만 제공해야 한다.
- 프로필은 런타임 경로와 Python 바이너리를 고정한다. 전체 라이브러리 공급망/컨테이너 이미지 서명 검증은 제공하지 않는다. 런타임 전체는 운영자가 별도로 승인한 불변 배포물로 준비해야 한다.

## 준비와 실행

`deploy/qa-foundation/worker.example.json`을 비공개 운영자 설정 위치에 복사하고 실제 검토한 SHA·경로로 채운다. 설정 파일에는 토큰을 넣지 않는다. 원본 소스는 깨끗한 전용 checkout, 런타임은 독립된 self-contained Python 설치여야 한다. 대상 suite가 요구하는 lxml 등 의존성은 승인된 런타임에 미리 준비한다. 이번 개발은 자원 생성이나 의존성 자동 설치를 하지 않는다.

```bash
# 1. 실행 환경에서 등록 소스와 격리 가용성만 점검한다. 실행권은 소비하지 않는다.
python scripts/configure_qa.py preflight --config /private/qa-worker.json \
  --project bellavita --release ledger-offline-v1

# 2. 소유자가 사전 검토한 계약과 프로필 digest를 기존 관리기 DB에 보관한다.
#    계약 생성은 이 CLI와 같은 검토된 checkout에서만 허용한다.
python scripts/configure_qa.py approve --config /private/qa-worker.json \
  --project bellavita --release ledger-offline-v1 \
  --database /private/manager.sqlite3 --owner-token-file /private/owner.token \
  --task PENDING_DEVELOPMENT_TASK_ID

# 3. 기존 POST /v1/qa/jobs로 approval/request_key/task를 접수한다.
# 4. 실행 호스트에서 별도 worker 토큰으로 한 작업을 실행한다.
python scripts/run_qa_worker.py --config /private/qa-worker.json \
  --token-file /private/qa-worker.token --job QA_JOB_ID
```

소유자·작업자 토큰 파일은 기존 `control.secret()`가 0600 권한과 일반 파일 여부를 검증한다. 작업자에는 프로젝트 범위의 worker 토큰만 사용한다. 프로필 경로가 달라지거나 바이너리/소스/릴리스/시드/한도가 바뀌면 새 프로필 digest와 새 승인이 필요하다. 기존 프로필 없는 승인은 하위 호환으로 남지만 새 격리 작업자는 이를 실행하지 않는다.

관리기 DB가 다른 호스트에 있으면 소유자 승인 절차는 그 관리기 호스트의 신뢰 코드에서 수행한다. 작업자가 제출한 프로필이나 보고서에서 사전 계약을 자동 승인하면 안 된다. 실제 연결을 위한 주소/계정 프로비저닝은 이번 범위에 포함하지 않는다.

## 추가 API와 복구

| API | 역할 | 처리 |
|---|---|---|
| `GET /v1/qa/jobs/{id}/execution` | 해당 프로젝트 owner/manager/worker | 고정 실행 manifest·상태·대조 기록 조회 |
| `POST /v1/qa/jobs/{id}/uncertain` | 실행권 worker | 고정 사유로 불확실 상태 기록 |
| `POST /v1/qa/jobs/{id}/reconcile` | owner | 독립 중단·외부 효과 확인과 증거 참조를 저장하고 `reconciled` 처리 |

`uncertain` 사유는 `execution_interrupted`, `result_delivery_uncertain`, `worker_restarted`만 허용한다. 서버/자식/토큰의 원문 오류를 저장하지 않는다.

```json
{
  "evidence_reference": "approved-observation:process-stop-and-effects-check",
  "process_stopped": true,
  "effects_absent": true
}
```

위 대조는 운영자가 실제 중단과 영향 부재를 확인한 뒤에만 보낸다. 외부 효과를 알 수 없으면 `effects_absent`를 true로 채우지 말고 불확실 상태를 유지한다. 대조 완료는 통과가 아니다. 새로 실행해야 한다면 소유자가 새 작업·필요시 새 계약을 승인한다.

결과 POST의 응답만 유실됐을 때는 한 번 GET하여 동일 보고서 digest가 이미 저장됐는지 대조한다. POST를 재전송하지 않는다. 실행권 응답이 유실되거나 이전 작업자가 종료됐으면 실행을 시작하지 않고 재시작 시 대조 상태를 남긴다. 만료 후 충돌 응답이 발생해도 만료 상태가 별도 트랜잭션으로 보존되도록 수정했다.

## 검증과 남은 연결

`manager_bridge.tests.test_qa_foundation`은 등록/소스 불일치, 격리 불가 시 미실행, 실제 HTTP 작업자 왕복, 실행 영수증 변조, 응답 유실, 재시작, 소유자 권한·대조 기록, 실제 테스트 프로세스의 시간/출력 제한과 종료를 검사한다. 테스트 실행기 주입은 명시적인 합성 fixture이며 실제 namespace 격리 성공 증거로 보고하지 않는다.

현재 관리형 환경에서는 bubblewrap UID map 생성이 제한되어 실제 격리 실행 경로가 준비되지 않을 수 있다. preflight의 실제 관찰을 별도 보존하고, 사용할 수 없는 경우를 통과로 표시하지 않는다. 승인된 실행 호스트에서 namespace 격리·런타임 준비를 확인한 뒤 실제 스테이징 서비스와 UI/외부 저장/실제 계산 계약을 적용해야 한다.

## 이번 실행 증거

실행 코드 리비전은 `2513cd0a1aad2deadb089b5ebebf1fcaddc11090`이다. 전체 회귀 **1,676개 실행·1,675개 통과·1개 skip·실패 0**, 새 기반 검사 16개 통과를 확인했다. 이전 검사와 겹치는 집중 검사 67개도 통과했으며 합계에 더하지 않았다.

`run-20261116-foundation/`에 preflight, 실제 작업자의 로컬 HTTP 미실행 관찰, 검사 결과와 해시를 보존했다. 실제 sandbox probe는 `available=false / isolation_verified=false`였다. 실제 작업자는 실행을 시작하지 않았고 SQLite 작업 상태는 `queued`로 유지됐다. 이 관찰은 격리 불가 시 차단 동작의 증거이며 성공적인 격리 실행 증거가 아니다. 성공 왕복·응답 유실·프로세스 종료 검사는 명시적인 fixture 경로로 확인했다.

같은 코드 리비전에서 기존 합성 3,006건과 실제 주문 정규화 함수의 제한된 오프라인 288건을 다시 통과했다. 결함 4종에서 각각 44/3/122/270건 실패를 검출하고 모두 차단했다. 전체 개별 증거와 수식 CSV를 이 실행 폴더에 별도로 보존했다. 이 재실행은 namespace 격리 성공 검증을 대체하지 않는다.
