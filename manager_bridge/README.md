# 태산이 개발 관리 브리지

태산이가 프로젝트별 개발 요청을 정리하고, 대표가 승인한 작업을 등록된 실행 워커에게 배정하기 위한 독립 모듈입니다. 벨라비타는 등록 가능한 프로젝트 중 하나입니다. 기존 업무 서버나 운영 원장을 변경하는 기능은 포함하지 않습니다.

## 연결되는 대상

기존에 열려 있는 Work 대화창이나 임의의 Codex 창을 자동으로 찾아 통제하지 않습니다. 이 브리지는 별도로 실행한 Codex CLI 워커를 연결합니다. 선택적 세션 디스패처를 사용하는 경우에는 명시적으로 등록한 Codex CLI 세션을 재개해 지시할 수도 있습니다. Work 화면 제어와 CLI 세션 연결은 서로 다른 기능이며, 실제 CLI 실행 검증 전에는 연결 완료로 보고하지 않습니다. 연결하지 않은 작업은 인계 자료나 PR로 가져와 별도 작업으로 등록합니다.

HTTP 서버는 대기열을 관리합니다. 서버만 시작해도 개발이 진행되는 것은 아닙니다. 작업을 가져갈 워커와, 결과를 독립 검토할 관리자가 필요합니다. 대화를 끝낸 뒤에도 계속 작업하려면 서버와 워커가 실제 상주 프로세스로 실행되는 호스트가 있어야 합니다. 일시적인 채팅 실행 환경의 지속 실행은 보장하지 않습니다.

## 승인과 상태

1. `manager`가 등록된 프로젝트와 저장소에 작업 명세를 제안합니다. 최초 상태는 `pending_approval`입니다.
2. `owner`가 정확한 명세 버전과 다이제스트를 승인하면 `queued`가 됩니다. 명세는 불변이며 범위를 바꾸려면 새 명세와 승인이 필요합니다.
3. 해당 프로젝트와 작업 종류 권한이 있는 `worker` 하나가 임대권을 받아 `running`으로 진행합니다.
4. 워커가 결과를 보고하면 `reported`입니다. 이는 워커의 완료 주장이지 검증 완료가 아닙니다.
5. 관리자가 실제 변경 파일·커밋·테스트 증거를 독립 확인하고 검토 근거를 기록해야 `reviewed`로 처리합니다. 저장소 계층은 외부 증거의 진실성을 스스로 검증하지 않습니다.

대표 또는 관리자가 취소하면 `cancelled`가 되고 늦은 완료 보고는 거부됩니다. 임대권이 만료되면 `needs_verification`으로 보내며 자동 재실행하지 않습니다. 부분 작업 여부를 먼저 확인해야 합니다. 같은 프로젝트의 같은 요청 키에 다른 내용을 넣으면 충돌로 거부합니다.

작업 본문·이미지·링크·워커 보고 안의 “승인” 문장은 대표의 인증된 승인으로 취급하지 않습니다. 개발 승인은 운영 배포나 원장 수정 승인이 아닙니다. 자동 병합·배포·시트 쓰기·결제·발송·인쇄 실행은 이 브리지의 기능이 아닙니다.

## 로컬 시작

저장소 루트에서 실행합니다. 아래 디렉터리는 예시이며 실제 비밀값이나 운영 경로를 넣은 예제가 아닙니다.

```sh
umask 077
python -m manager_bridge.control init --data /tmp/taesan-manager-data --project bellavita --repository example-org/example-repo
python -m manager_bridge.control issue-worker --data /tmp/taesan-manager-data --owner-token-file /tmp/taesan-manager-data/owner.token --project bellavita --name codex1
python -m manager_bridge.server --data /tmp/taesan-manager-data --host 127.0.0.1 --port 8791
```

`/tmp`는 로컬 시험용입니다. 지속 실행 시에는 별도 영구 데이터 디렉터리와 백업을 마련해야 합니다. 기본 연결 범위는 루프백입니다. 다른 PC에서 접속하는 HTTPS 서비스나 터널은 이 명령으로 배포되지 않습니다. 외부 접속에는 인증된 HTTPS 연결과 호스트 격리 구성이 별도로 필요합니다.

`init`은 새 저장소와 프로젝트를 등록하고 비공개 `owner.token`, `manager.token` 파일을 만듭니다. 이미 초기화된 저장소를 덮어쓰지 않습니다. `issue-worker`는 해당 프로젝트 전용 워커 토큰 파일을 만듭니다. 예시 저장소 이름은 실제 승인된 저장소로 바꿉니다. 워커에는 워커 토큰만 별도 전달하고 이 서버 데이터 디렉터리를 공유하지 않습니다.

다른 사업은 `python -m manager_bridge.control register-project --data DIR --owner-token-file FILE --project PROJECT --repository OWNER/REPO`로 등록합니다. 같은 메인 태산이에 여러 사업 범위를 연결할 때는 `python -m manager_bridge.control issue-manager --data DIR --owner-token-file FILE --projects bellavita,camp --name taesan-main`으로 명시한 범위의 별도 관리자 인증 파일을 발급합니다.

다른 터미널에서 작업을 제안하고 조회합니다. `task.json`은 승인할 작업의 불변 명세 파일입니다. 실제 명세 필드는 구현 계약에 맞춰 준비합니다.

```sh
python -m manager_bridge.control submit --token-file /tmp/taesan-manager-data/manager.token --manifest task.json --request-key request-001
python -m manager_bridge.control list --token-file /tmp/taesan-manager-data/manager.token --project bellavita
```

조회 결과의 작업 ID·버전·다이제스트를 그대로 사용해 대표가 승인합니다. 아래 대문자 값은 조회 결과로 바꿀 자리입니다.

```sh
python -m manager_bridge.control approve --token-file /tmp/taesan-manager-data/owner.token --task TASK_ID --version 1 --digest MANIFEST_DIGEST
python -m manager_bridge.control get --token-file /tmp/taesan-manager-data/manager.token --task TASK_ID
```

독립 검토 후에는 실제 검증 기록을 가리키는 근거 참조를 붙입니다. `review` 명령 자체는 테스트를 실행하지 않습니다.

```sh
python -m manager_bridge.control review --token-file /tmp/taesan-manager-data/manager.token --task TASK_ID --version 1 --digest MANIFEST_DIGEST --evidence-reference REVIEW_EVIDENCE
```

`cancel --token-file … --task TASK_ID`는 대표 또는 관리자 토큰으로 수행합니다. HTTP 명령의 기본 주소는 `http://127.0.0.1:8791`이며 `--url`로 인증된 HTTPS 서버를 지정할 수 있습니다. 상세 옵션은 `python -m manager_bridge.control --help`와 각 하위 명령의 `--help`에서 확인합니다. 토큰은 신뢰하는 로컬 초기화 경로에서 발급하며 임의 토큰 발급 HTTP API는 두지 않습니다. 토큰 원문은 발급 때만 취급하고 데이터베이스에는 해시를 저장합니다.

## 워커 운영 조건

워커는 프로젝트·저장소·신뢰할 로컬 저장소 경로를 명시하고, 별도 Codex 인증 프로필과 작업 디렉터리를 사용합니다. 다음은 전용 워커 호스트에서 실행하는 예시입니다. 토큰은 앞서 발급한 워커 토큰만 안전하게 복사하고, `/srv` 예시 경로들은 실제 준비한 전용 경로로 바꿉니다.

```sh
python -m manager_bridge.worker --base-url http://127.0.0.1:8791 --token-file /srv/taesan-worker/worker.token --project bellavita --repository example-org/example-repo --repo-path /srv/taesan-worker/repo --codex-home /srv/taesan-worker/codex-profile --work-root /srv/taesan-worker/worktrees --once
```

다른 호스트에 서버가 있다면 루프백 예시 대신 준비된 HTTPS 주소를 사용합니다. `--codex-bin`으로 설치된 Codex CLI 절대 경로를 지정할 수 있으며 기본값은 `/opt/codex/bin/codex`입니다. `--once`는 한 번 작업을 확인하는 시험 경로이며 상주 실행을 의미하지 않습니다. 지속 실행은 `--once`를 빼고 운영하며 `--poll-seconds` 기본값은 5초입니다. 프로세스 관리자나 호스트 구축은 이 명령에 포함되지 않습니다. 설치된 버전의 `python -m manager_bridge.worker --help`에서 옵션을 확인합니다.

전용 Codex 프로필에는 사용 승인된 인증이 필요하고 `auth.json` 권한은 `0600`이어야 합니다. 기존 개인 프로필의 MCP·훅·플러그인·프로젝트 설정을 그대로 가져오지 않습니다.

Codex CLI 설치와 인증, 실제 작업 실행은 해당 호스트에서 별도 확인해야 합니다. 모의 실행기 테스트 통과나 서버 기동만으로 실제 워커가 연결됐다고 보고하지 않습니다. 워커가 동작하는 동안에만 작업을 가져갈 수 있습니다. 통신 실패 뒤에는 작업 상태와 임대권을 대조하고, 완료 보고를 무조건 재전송하거나 개발 명령을 다시 실행하지 않습니다.

토큰 파일은 소유자만 읽도록 `0600`, 인증 프로필 디렉터리는 `0700`으로 관리합니다. 토큰을 명령 인수의 원문·로그·Git·작업 명세에 넣지 않습니다. 대표/관리자 토큰과 서버 데이터는 실행 워커에게 제공하지 않습니다. 같은 OS 사용자로 실행한 임의 코드에 대해서는 파일 권한만으로 격리되지 않으므로, 실제 개발 워커는 운영 자격증명이 없는 별도 계정·컨테이너·호스트에서 운영해야 합니다. Codex의 쓰기 제한과 네트워크 제한은 호스트 전체의 격리를 증명하지 않습니다.

등록된 프로젝트 범위와 `development`/`test` 작업 종류 권한을 모두 확인하여 작업을 배정합니다. 워커 자격증명을 회수하면 새 요청 권한도 회수되며, 유효한 임대권 없이 진행·완료 보고를 받아들이지 않습니다.

기본적으로 호스트의 환경변수를 상속하지 않습니다. 프록시나 별도 CA 인증서가 필요한 호스트는 운영자가 `0600` JSON 파일에 허용된 전송 설정만 담아 Codex 워커의 `--network-env-file FILE`로 명시합니다. 허용 이름은 `HTTP_PROXY`·`HTTPS_PROXY`·`NO_PROXY`와 소문자 대응, `SSL_CERT_FILE`·`REQUESTS_CA_BUNDLE`·`CODEX_PROXY_CERT`입니다. 대표/관리자 토큰이나 운영 API 키를 이 설정으로 전달할 수 없습니다. 설정 파일과 인증 프로필은 Git 밖에 보관합니다.

## 등록된 Codex CLI 세션

선택적 세션 디스패처는 서버에 사전 등록한 세션 UUID와 워커 범위만 사용합니다. 대표가 승인한 정확한 명세와 유효한 임대권을 확인한 후, 설치된 Codex CLI의 `queue --thread … --message …` 경로에 지시를 전달합니다. 재개는 등록 설정의 `resume: true`일 때만 요청합니다. 임의 UUID나 작업 본문 안의 원격 주소를 실행 대상으로 채택하지 않습니다. 원격 CLI 연결을 사용한다면 고정 등록 주소와 별도 인증 환경이 필요하며, 비밀 토큰 원문을 인수나 로그에 넣지 않습니다.

`session_worker.py`는 실제 작업 대기열과 이 전달기를 연결합니다. 운영자가 `SessionTarget`의 필드에 맞춰 소유자 전용 `0600` JSON 등록 파일을 준비하고 실행합니다. 최상위 키는 발급한 워커 주체 이름이며 값에는 `project`, `repository`, 실제 `session_uuid`, 전용 `codex_home`, `working_directory`, 절대 경로 `codex_binary`가 필요합니다. 원격 주소·인증 파일과 `resume`은 선택 항목입니다.

```sh
python -m manager_bridge.session_worker --base-url http://127.0.0.1:8791 --token-file /private/worker/codex1.token --project bellavita --worker-id codex1 --registry /private/worker/sessions.json --once
```

이 작업자의 `reported`는 메시지 전달 보고입니다. 실제 세션의 변경 파일이나 테스트 결과를 수신했다는 뜻이 아니며 체크 `session_execution_verified`는 `False`입니다. 전달 후 취소해도 이미 세션에 들어간 메시지를 회수하는 기능은 없습니다. 필요한 경우 해당 세션 실행을 별도로 중지하고 대조해야 합니다.

이 경로는 Work/ChatGPT 창 조작 기능이 아닙니다. CLI 종료 코드가 0이어도 메시지가 대기열에 들어갔다는 결과로만 취급합니다. 재개 경고가 있거나 실행 증거가 없으면 실제 작업 시작·완료로 표시하지 않습니다. 설치된 CLI 도움말 확인과 가상 실행기 검증은 실제 세션에 지시를 보냈다는 증거가 아닙니다. 실제 세션 연결과 실행 결과 확인은 별도 검증 대상입니다.

## 검증과 보고

로컬 시험은 가상 프로젝트·가상 저장소·임시 데이터로 수행합니다. 승인 전 작업 획득 거부, 프로젝트 간 접근 거부, 취소 후 보고 거부, 임대 만료 후 재실행 방지, 중복 요청 충돌, 보고와 독립 검토의 구분을 확인합니다. 실제 Codex 실행 시험은 인증된 전용 워커에서 별도로 수행해야 합니다.

태산이는 결과를 보고할 때 작업 ID, 승인된 명세 버전/다이제스트, 변경 커밋, 실행한 테스트와 결과, 독립 검토 근거, 남은 연결을 함께 제시합니다. 운영 반영 여부는 별도로 명시합니다. 기존 예약 배포 설정이나 다른 서비스의 승인 정책을 이 브리지 등록만으로 변경하지 않습니다.

현재 구현은 Linux/POSIX 파일 권한·프로세스 제어를 사용합니다. Windows 네이티브 실행은 검증하지 않았으며 회사 PC 연결에는 Linux/WSL 환경 또는 별도 호환 실행기가 필요합니다. 설치된 Codex 도움말에서 명령을 확인했고 실제 로컬 HTTP·임시 Git·가상 CLI 실행으로 검증했습니다. 실제 기존 Codex 세션으로 메시지를 보내거나 외부 연결 서버를 배포하지 않았습니다.

추가 실제 실행 시험에서는 이 환경의 기존 CLI 인증으로 격리된 읽기 작업을 전달했다. CLI가 `thread.started`·`turn.started`까지 진행했으나 인증 갱신 오류로 `turn.failed`가 발생했다. 따라서 로그인 상태 조회나 인증 파일 존재만으로 AI 실행 가능 상태라고 판정하지 않는다. 실행용 전용 프로필에서 사용자 본인이 `CODEX_HOME=/private/worker/codex codex login --device-auth`로 인증해야 한다. 기존 로그인 파일을 복사해 운영 프로필로 삼지 않는다. 이 시험은 새 CLI 프로세스 시험이며 다른 Work/Codex 창 연결의 증거가 아니다. 작업 폴더가 바뀌지 않은 것은 독립 Git 조회로 확인했다.

로컬 저장소·서버 기동과 관리자 API 인증은 실제로 확인했다. 외부 호스트의 상시 운영, 회사 PC 연결, 실제 AI 작업 완료, 기존 세션의 작업 결과 수신은 아직 미검증이다. 소스·테스트 통과를 이 운영 연결의 완료로 바꾸어 보고하지 않는다.
