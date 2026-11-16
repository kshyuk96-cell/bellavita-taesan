# 재시작 진단과 로컬 관리자 중복 실행 가드

기존 연속 관리자는 DB 리더 epoch/nonce, 작업 intent/receipt, 불확실 동작 격리, 기존 임대·전역 한도, 복구본 실행 차단을 사용합니다. Codex 전송은 메타데이터·실행 제한 시간을 이미 갖고 있습니다. 이번 보완은 이 정책을 대체하지 않고 **프로세스 수명 동안의 로컬 관리자 단일 실행**과 **흩어진 재시작 상태의 통합 진단**을 제공합니다. 실제 모델 호출, 운영 작업 시작, 배포 또는 Tester 엔진을 실행하지 않습니다.

## 서버 조립 인터페이스

```python
from manager_bridge.recovery_readiness import RecoveryReadiness
recovery = RecoveryReadiness(core, controller=controller, supervision=supervision)
state = recovery.inspect(manager_token, ['registered-project'], limit=20)

# 실제 서비스의 단일 실행 진입점에서 한 번만 획득합니다.
with recovery.instance_guard(manager_token, ['registered-project'], 'registered-instance'):
    trusted_manager_loop()
```

`trusted_manager_loop`는 기존에 등록된 관리자 루프를 나타내는 조립 위치입니다. HTTP나 사용자가 임의 함수를 전달하는 인터페이스가 아닙니다. canonical 서비스가 이미 가드를 획득한다면 바깥에서 다시 획득하지 않습니다. 루프·HTTP serve thread·정기 감독 관찰을 시작하기 전에 가드를 잡고 종료 정리가 끝날 때까지 유지합니다. 진단·보고 API 자체는 작업자를 시작하지 않습니다.

## 실제 부족했던 경계

기존 tick 임대는 실제 업무 dispatch를 보호하지만 두 관리자 프로세스가 서로 다른 포트로 떠서 소스·감독 관찰을 중복 수행하는 것까지 프로세스 수명 전체에 걸쳐 제한하지는 않았습니다. `instance_guard`는 등록된 DB의 비공개 부모에 DB 이름에서 만든 고정 가드 파일을 열어 `flock(LOCK_EX|LOCK_NB)`를 유지합니다. 같은 로컬 DB의 두 번째 협조적 관리자는 즉시 거절합니다.

가드 파일은 0600·현재 사용자 소유·빈 정규 파일·단일 링크여야 합니다. 부모는 0700이고 DB와 조상 경로의 심볼릭 링크를 거절합니다. 파일 내용에 PID, 토큰, 프로젝트 경로나 제공자 응답을 쓰지 않습니다. 재시작 때 파일이 남아 있다는 이유로 활성 프로세스를 판단하지 않습니다. OS가 보유한 lock만 사용하며 프로세스 종료·충돌 뒤 lock은 해제됩니다. 파일을 임의 삭제하거나 다른 inode로 교체해 가드를 우회하지 않습니다.

이는 같은 로컬 저장소의 협조적 관리자용 가드입니다. 분산 호스트 전체의 리더 선출, 실제 작업자의 종료, 운영 호스트의 상시 가동 또는 악의적인 같은 UID 프로세스를 증명하지 않습니다. 기존 DB 리더·nonce·예산·프로필 가드·권한 검사는 계속 필요합니다.

## 멈춤과 불명확 상태를 구분하는 진단

`inspect`는 소유자/관리자와 명시한 최대 100개 프로젝트의 설정 범위를 검증합니다. 다음 기록을 정규화하되 내용이나 프로세스를 변경하지 않습니다.

- 복구 마커가 남아 있는 DB: 기존 실행 격리를 그대로 보고합니다.
- `uncertain` 연속 관리자 동작: 명시적인 조정이 필요한 상태입니다.
- 리더가 없거나 만료되었거나, 호출자·정확한 프로젝트 집합·현재 태산 회원 바인딩·동작 actor/epoch와 일치하지 않는데 남아 있는 `running` 동작: 실행 중인지 중단되었는지 추측하지 않습니다.
- 종료 확인 없이 만료된 개발자 임대, 만료된 독립 리뷰 임대: 응답 없음과 종료 증거를 구분합니다.
- `uncertain`/`freezing` 호스트 할당 및 현재 리더 및 같은 task의 running actor/epoch 증거가 없는 `executing` 할당: 후보나 성공 receipt를 만들지 않습니다.
- 불확실 감독 작업 또는 기한을 넘긴 `running` 감독 작업: 자동 재개하지 않습니다. 공유 감독 정보는 감독의 전체 프로젝트 범위를 인증한 호출자만 볼 수 있습니다.

`dispatch_ready: false`와 `blockers`는 보수적인 재시작 진단입니다. `true`여도 업무·기술 승인, 현재 소스·QA·연결·호스트 준비, 실행 한도를 대신하지 않습니다. 정상 진행 중인 유효한 임대 자체를 실패나 종료로 표시하지 않습니다. `workers_stopped_verified`, `automatic_replay`, `always_on_verified`는 항상 false입니다. DB 행을 재대기열로 바꾸거나 lease를 갱신하거나 복구 격리 마커를 해제하지 않습니다.

상세 표시 기본값은 20건, 최대 100건이며 각 종류의 전체 건수는 SQL 집계로 계산합니다. 상세 창이 잘리면 `details.partial`이 true입니다. 일부 상세가 생략되어도 전체 집계에서 발견한 차단 사유는 없어지지 않습니다. `inspect`는 PID, 원시 오류·로그, 토큰·해시 또는 비공개 경로를 반환하지 않습니다.

## fixture로 확인한 범위

새 테스트는 불확실 동작을 남긴 DB를 다시 열어도 start callback이 재호출되지 않는지, 응답 없는 임대 관찰이 작업을 종료/재대기열 처리하지 않는지, 복구 마커·호스트 불확실 할당·감독 미완료를 유지하는지 확인합니다. 실제 Python fixture 프로세스 두 개로 로컬 가드 충돌을 확인하고 가드를 보유한 fixture 프로세스를 종료한 뒤 재획득합니다. 이 검증은 실제 운영 작업자·모델·계정에 접근하지 않으며 하루 동안의 가동 보장이 아닙니다.
