# 프로젝트별 테스트 수신기

이 모듈은 기존 승인된 작업 대기열을 받는 **명시적 테스트 실행기**다. 기존 Codex 창의 대화나 임의 개발 명령을 원격 조작하는 기능은 아니다. 운영 배포·결제·발송·시트 수정·계정 생성·스케줄러는 포함하지 않는다.

## 고정 실행 계약

`ProjectTestProfile`에는 프로젝트·저장소·정확한 소스 커밋·소스 경로·전용 런타임·선택한 읽기 전용 의존성 경로·제한 시간이 들어간다. `ProjectReceiver`는 기존 worker 인증과 등록부의 활성 범위를 확인한다. 태스크는 `kind=test`와 프로젝트·저장소·기준 커밋이 일치해야 한다. 지시문·링크·임의 명령은 실행 인수로 쓰지 않는다.

| 프로젝트 | 등록된 검사 |
|---|---|
| 벨라비타 쇼핑몰 | `python_tests`: Python unittest |
| 벨라비타 ERP | `build`, `node_tests`: 실제 package.json의 build/test; 테스트 전 빌드 강제 |
| 희망일출 | `build`, `node_tests`: 실제 package.json의 build/test |
| 장례지도사협회 | `node_tests`, `typecheck`, `build`: 실제 package.json의 test/typecheck/build |

위임 작업에 필요한 `authorization_regression`, `approval_regression`은 일반 단위시험의 별칭이 아니다. 별도의 정확한 관리 코드 `ControlChecks` 소스/커밋/런타임을 등록해야 한다. 전자는 작업자·수신·중단 권한 회귀, 후자는 단일/묶음 위임 승인 회귀를 각각 실행한다. 해당 소스나 매핑이 없으면 `missing_required_control_checks`로 멈춘다.

## 격리와 복구

기본 엔진은 `/usr/bin/bwrap --unshare-all`을 사용한다. 호스트 `/home`, `/workspace`, 인증 상태, 네트워크를 전달하지 않는다. 원본은 정확한 Git 커밋의 정규 파일만 새 작업 폴더에 복사하며 `.git`과 무시된 로컬 데이터는 복사하지 않는다. 런타임과 등록 의존성은 읽기 전용이다. 명령별 시간·파일 크기·열린 파일·프로세스 수·주소 공간 제한이 있다. 표준 출력/오류는 저장하거나 보고하지 않고 등록 검사명과 종료 결과만 보고한다.

격리 시작 시험에 실패하면 작업을 claim하지 않으며 다른 실행 방식으로 후퇴하지 않는다. 이 Codex 환경의 실제 시험에서는 네트워크 네임스페이스를 만들 때 NETLINK_ROUTE 소켓 권한이 거부되어 **`sandbox_unavailable`**이었다. 격리 고객 테스트 실행은 아직 입증되지 않았다. 모의 엔진 테스트 통과는 실제 격리 성공으로 기록하지 않는다.

매 검사 중 heartbeat가 인증·등록·활성 범위·임대를 다시 확인한다. 취소·권한 철회·시간 초과 시 실행 프로세스 그룹을 중지하고 기존 대기열의 `needs_verification` 또는 취소 상태를 보존한다. `recovery()`는 읽기 전용 상태 대조 결과만 반환한다. 중단 증거/독립 검증 전 재실행이나 저장소 잠금 해제는 하지 않는다. 결과는 worker 주장이고 관리자의 독립 검증을 대신하지 않는다.

## 로컬 CLI

`python scripts/run_project_receiver.py --database PRIVATE_DB --token-file PRIVATE_WORKER_TOKEN --profile PRIVATE_PROFILE --agent REGISTERED_AGENT`는 기본적으로 점검만 한다. 세 파일은 기존 비공개 파일이어야 하며 인증 내용을 출력하지 않는다. `--run --max-cycles N`을 명시하면 승인된 등록 inbox만 최대 100회 순차 확인하고, 보고 완료 외의 상태에서 멈춘다. `--recover`는 중단 상태만 대조한다. 데몬이나 예약 작업은 시작하지 않는다.

프로필 JSON은 `project`, `repository`, `base_sha`, `source`, `runtime` 필수, `dependencies`, `timeout`, `control` 선택이다. `control`은 `source`, `base_sha`, `runtime`만 받는다. 런타임은 `/runtime/bin/python` 또는 `/runtime/bin/npm`에서 실제로 실행 가능한 전용 구성으로 준비해야 한다. 현재 PC 런타임 디렉터리를 무작정 노출하거나 운영 자격증명을 복사하지 않는다. 실제 등록·호스트·의존성 준비와 운영 변경은 기존 승인 범위에 따라 별도로 진행한다.

Node/V8가 요구하는 가상 주소 공간은 현재 1.5GiB 주소 공간 제한보다 클 수 있다. 실제 Node 격리 빌드와 런타임 경로는 아직 검증되지 않았으며, 전용 호스트의 자원 계획과 함께 제한값을 검토해야 한다. 현재 제한을 완화하거나 격리를 끄는 방식으로 시험을 우회하지 않았다. 관리 코드도 변경 없는 정확한 커밋으로 고정한 뒤 `ControlChecks`에 등록해야 한다.
