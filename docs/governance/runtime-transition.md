# 시작·종료·재시작·업데이트 전 drain

`RuntimeTransition`은 새 업무의 **입장(admission)**을 막는 영속 상태입니다. 프로세스 종료 기능이나 기존 작업자의 종료 증거가 아닙니다. 기존 리더 epoch/nonce, 로컬 관리자 수명 가드, 실행 intent/receipt, 프로필 가드, 복구 격리와 예산·호스트 검사를 그대로 사용합니다. 이 작업은 운영 서비스의 시작·정지·설치·배포, 원격 연결, 요금 변경 또는 Tester 엔진을 수행하지 않습니다.

## 소유자 결정과 실제 실행 경계

```python
from manager_bridge.runtime_transition import RuntimeTransition
transition = RuntimeTransition(core, supervision=registered_supervision)
state = transition.status(manager_token, ['registered-project'])
revision = state['projects'][0]['revision']
transition.drain(owner_token, ['registered-project'],
                 {'registered-project': revision}, 'planned-update-001')
```

`drain(token, projects, expected_revisions, request_key, reason='planned_update')`와 `resume(token, projects, expected_revisions, request_key)`는 프로젝트 범위가 있는 소유자만 호출합니다. 사유는 `planned_update`, `planned_restart`, `planned_stop`으로 제한하며 비밀이나 임의 명령을 받지 않습니다. 전체 프로젝트의 revision을 먼저 확인하고 한 트랜잭션에서 갱신합니다. 오래된 revision, 다른 범위, 바뀐 설정 또는 이미 다음 결정으로 대체된 요청 키는 거절합니다. 같은 아직 유효한 요청 키의 재호출은 같은 결정 receipt를 반환합니다.

새 `assert_new_start(c, project)`는 실제 업무 입장 트랜잭션에 연결됩니다. `ThreeTier._admission`의 무조건 검사와 `OperatingBudget._check`의 읽기 판단 경계에서 실행하므로 예산 서비스가 없거나 오래된 HTTP 경로를 사용해도 drain을 우회해 새 작업을 시작할 수 없습니다. 설치한 상태가 없으면 기존 동작을 유지합니다. 한 번 기록된 drain은 core·서비스를 재구성해도 남습니다. 설정 fingerprint가 바뀐 상태도 소유자 CAS 검토 전에는 fail closed입니다.

이미 입장한 업무의 개발 진행, 보고, 수집, 독립 리뷰와 기술 판단을 막지 않습니다. 새로운 `start`와 부모 승인 후 새 자식 배정은 차단합니다. drain 전에 이미 배정된 `development_queued` 업무는 기존 흐름으로 마무리할 수 있습니다. 소유자 `resume`은 입장 제한만 해제하며 기술 승인, 새 자격 증명, 기존 불확실 실행의 재전송이나 종료 확인을 주지 않습니다.

## 업데이트 전 관찰 순서

1. 등록된 서비스 전체 프로젝트의 현재 revision·범위를 확인하고 drain을 요청합니다.
2. 기존 수집·보고·리뷰가 끝나도록 둡니다. `status`는 입장한 hierarchy 업무, 종료 확인이 없는 worker 업무, running/uncertain 연속 동작·호스트 할당을 집계합니다. 공유 감독 서비스가 등록되면 그 전체 범위를 인증한 호출자만 running/uncertain 감독 작업을 확인할 수 있습니다.
3. 요청한 모든 프로젝트가 `draining`이고 현재 설정에 묶여 있으며 위 기록이 없을 때만 `drain_ready`가 true입니다. 활성 임대가 만료되거나 PID가 사라졌다는 이유로 true가 되지 않습니다. 승인되었지만 아직 입장하지 않은 대기 작업은 새 시작이 차단된 채 남습니다.
4. `drain_ready`는 선택한 범위의 기록상 준비 상태일 뿐 `workers_stopped_verified`나 `host_update_authorized`를 true로 만들지 않습니다. 서비스 전체 범위를 빠뜨리지 말고 새 백업·구독 갱신 등 감독 작업이 업데이트와 겹치지 않도록 등록된 운영 절차로 다룹니다.
5. 실제 서비스 정지·업데이트·시작은 별도로 권한을 가진 운영자가 검토한 절차로 수행해야 합니다. 애플리케이션 업데이트 후에는 깨끗한 새 커밋과 private host config의 기대 커밋, 지속 상태·역할·격리·연결 검사를 다시 확인합니다. 이 모듈은 코드나 호스트 설정을 교체하지 않습니다.
6. 원래 실행의 결과가 불명확하면 먼저 기존 조정 절차를 사용합니다. `resume`을 눌렀다는 이유로 불확실 기록·복구 마커를 제거하지 않습니다. 새 admission은 기존 소유자 업무 승인, 예산·현재 인증·호스트·소스·QA 조건도 계속 통과해야 합니다.

`status`는 논리적인 운영 준비 상태만 반환합니다. `workers_stopped_verified`, `host_update_authorized`, `automatic_replay`는 항상 false입니다. PID·시간 초과·프로세스 exit code만으로 외부 작업이 종료되었다고 추정하지 않습니다.

## Linux systemd 초안 생성

아래는 **검토 파일 생성 명령**이며 시스템에 설치하거나 서비스를 작동시키지 않습니다. `--output` 부모는 현재 사용자 소유의 0700 비공개 디렉터리여야 합니다. 출력은 새 0600 파일이며 기존 파일·심볼릭 링크를 덮어쓰지 않습니다.

```bash
python scripts/prepare_governance_unit.py \
  --application-root /opt/taesan/app \
  --python /opt/taesan/venv/bin/python \
  --config-file /var/lib/taesan/governance.json \
  --state-directory /var/lib/taesan \
  --output /private/review/taesan-governance.service
```

생성기는 고정된 비-root 사용자·그룹과 안전한 절대 경로만 받습니다. 줄바꿈, systemd `%` 치환, 쉘 인수, 공백 경로와 `..`를 거절합니다. 설정 파일을 읽거나 토큰을 복사하지 않습니다. `/etc/systemd`, `/usr/lib/systemd`, `/lib/systemd`, `/run/systemd` 아래로 출력하는 것도 거절합니다. `systemctl`, 계정 생성, daemon-reload, enable, start, stop, restart 또는 kill을 호출하지 않습니다.

초안은 foreground 관리자 스크립트, 0077 umask, 영속 상태 mount 요구, 실패 시 제한된 재시작, SIGTERM, control-group 종료와 60초 종료 제한을 지정합니다. 이 설정은 기존 감독 예시를 구체화한 초안입니다. `TimeoutStopSec` 이후 systemd가 프로세스 그룹을 강제 종료할 수 있어도 원격·외부 작업자의 종료가 증명되지는 않습니다. 다음 시작은 기존 불확실 동작·임대·프로필 가드와 로컬 관리자 수명 가드를 다시 확인해야 합니다.

## 확인한 실행 범위

테스트는 별도의 비밀 없는 SQLite fixture와 Python child 서비스만 사용합니다. fixture child에 SIGTERM을 보내 정상 종료를 확인하고, 다른 fixture를 SIGKILL한 뒤 기존 관리자 수명 가드를 다시 획득하여 재시작합니다. 별도 fixture가 영속 drain을 관찰해 종료하는 것도 확인합니다. core start 및 새 자식 배정은 drain으로 막히고, 이미 실행한 fixture의 보고·수집·리뷰·기술 승인은 끝까지 진행됩니다. 실제 모델·구독 로그인·고객 업무·운영 서비스는 시작하거나 종료하지 않습니다. 이 검증은 상시 호스트나 24시간 가동의 증거가 아닙니다.
