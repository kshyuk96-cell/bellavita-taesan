# 비공개 결과물 본문 보관·조회

`ArtifactContent`는 등록된 호스트 관찰자가 실제 읽은 결과물 bytes를 비공개 디렉터리에 보관합니다. 내용은 데이터이며 명령·Python·테스트·프롬프트로 실행하거나 해석하지 않습니다. 이 기능은 Tester 엔진, 네트워크 연결, 운영 배포 또는 클라우드 자원을 추가하지 않습니다. 본문 보관과 SHA 확인은 QA 통과나 업무·기술 승인 권한을 주지 않습니다.

## 신뢰된 호스트 등록

```python
from manager_bridge.artifact_content import ArtifactContent
from manager_bridge.operations_reporting import ArtifactObservation

# trusted_results는 이미 등록된 프로젝트 결과물 저장소입니다.
# key를 파일 경로나 URL로 변환하는 범용 업로드/다운로드 기능이 아닙니다.
def observe_qa(project, key):
    data, observed_at = trusted_results.read_registered(project, key)
    return ArtifactObservation(project, 'qa_report', key, data, observed_at)

content = ArtifactContent(core, '/private/taesan/artifact-content',
    sources={('registered-project', 'qa_report'): observe_qa})
metadata = content.retain(manager_token, 'registered-project', 'qa_report',
                          'result-001', retention_seconds=86400)
body = content.read_body(manager_token, metadata['reference'])  # 로컬 Python 전용
```

위 경로는 서버 운영자가 등록하는 기존 디렉터리입니다. 요청자가 호스트 경로, URL, 코드, callback, 업로드 bytes 또는 주장한 SHA를 전달하는 API는 제공하지 않습니다. 관찰자가 없으면 `NotFound`로 차단합니다. 허용 종류는 기존 메타데이터 표준의 `candidate`, `review_evidence`, `qa_report`, `backup_manifest`, `diagnostic`입니다.

관찰 결과는 `ArtifactObservation`이며 최대 8 MiB bytes와 실제 관찰 시각을 포함합니다. 현재 기준 300초보다 오래되거나 미래인 관찰은 거절합니다. 서버가 bytes의 SHA-256과 실제 크기를 계산합니다. 등록된 소스 호출 전과 후에 요청자의 소유자/관리자 역할, 프로젝트 범위와 설정을 확인합니다. 다른 프로젝트의 사용자나 작업자 역할은 본문을 읽을 수 없습니다. 동일 프로젝트·설정·종류·키의 보관 내용은 불변입니다. 논리적으로 만료된 키를 다시 호출해 보관 기간을 자동 연장하지 않습니다.

## 파일과 비밀 정보 보호

본문 디렉터리는 현재 호스트 사용자 소유의 0700이어야 하고, 조상 경로를 포함해 심볼릭 링크를 허용하지 않습니다. 디렉터리의 device/inode를 고정합니다. 공개 경로로 바꾸거나 다른 디렉터리로 교체하면 거절합니다. 새 본문은 무작위 불투명 참조 이름을 사용한 0600 파일입니다. 임시 파일을 fsync한 뒤 Linux `renameat2(RENAME_NOREPLACE)`로 원자적으로 공개하며 기존 파일을 덮어쓰거나 hardlink를 생성하지 않습니다. 지원되지 않는 플랫폼에서는 안전하지 않은 대체 동작으로 진행하지 않습니다.

기본 용량은 디렉터리 전체 64 MiB입니다. 신뢰된 생성자 설정은 8 MiB~1 GiB 범위로 제한합니다. 파일 수는 최대 4096개이며 출처 불명·논리 만료 파일도 용량에 포함합니다. 알려지지 않은 파일을 삭제해 공간을 확보하지 않습니다. 디렉터리에 심볼릭 링크, hardlink, 잘못된 소유권·권한, 너무 큰 파일 또는 비정규 파일이 있으면 보관을 차단합니다. 프로세스 중단으로 생긴 공개 후 미기록 파일은 비공개로 남고 용량에 포함됩니다. 현재 작업의 아직 공개하지 않은 임시 파일만 실패 시 정리합니다.

등록된 모든 인증 해시와 비교하는 제한된 부분 문자열 검사를 통해 다른 역할의 알려진 토큰도 거절합니다. 현재 요청 토큰은 전체 bytes에서 직접 확인하며, binary 구분자와 ASCII 접두·접미사에 붙은 토큰도 검사합니다. Bearer/Basic 인증, 일반 API·GitHub·Slack·AWS 키, JWT, private key와 구독 refresh/access/id token 필드 등 강한 비밀 패턴도 거절합니다. 원시 값이나 거절된 bytes를 로그·예외 메시지·DB에 기록하지 않습니다. 정상 SHA 문자열 자체를 비밀로 간주하지 않습니다. 검사량은 등록 토큰 10000개, 연속 printable ASCII 2048 bytes, 부분 문자열 hash 검사 100000회로 제한하며 한도 초과는 실패로 처리합니다. 비밀 탐지 규칙은 모든 형태의 민감 정보를 완벽히 판별하는 도구가 아니므로, 제공자는 애초에 자격 증명 파일이나 원시 인증 로그를 관찰 대상으로 등록하면 안 됩니다.

## 로컬 본문과 메타데이터의 구분

`metadata(token, reference)`는 SHA, 크기, 관찰·만료 시각과 범위가 확인된 불투명 참조만 반환합니다. 파일 경로, 원시 내용과 root inode를 반환하지 않습니다. 메타데이터의 `retrieval`은 `metadata_only`, `body_access`는 `privileged_local_only`입니다. **HTTP 본문 다운로드 경로는 제공하지 않습니다.** 상위 제어 API는 메타데이터만 노출해야 합니다. 향후 HTTP 제공이 필요하면 콘텐츠 형식, 안전한 파일 다운로드 framing과 별도 접근 정책을 먼저 설계해야 합니다. 본문을 HTML이나 실행 가능한 응답에 삽입하지 않습니다.

`read_body`는 인증된 로컬 호스트 Python 호출에만 최대 8 MiB bytes를 반환합니다. 파일을 `O_NOFOLLOW`로 열고 소유자·0600 권한·단일 링크·정규 파일·크기·고정 inode를 확인합니다. 읽은 bytes의 SHA를 메타데이터와 비교하고 반환 전에 현재 인증·프로젝트 설정·논리 만료·비밀 검사를 다시 수행합니다. 읽는 사이 인증이 해제되거나 내용이 바뀌면 반환하지 않습니다.

보관 기간은 60초~30일, 기본 1일입니다. 만료 뒤 본문은 접근할 수 없지만 자동 물리 삭제는 하지 않습니다. `physical_deletion_verified`와 `acceptance_authorized`는 항상 false입니다. 파일을 없앴거나 작업자를 중지했다고 추측하지 않습니다.

기존 `OperationsReporting`과 연결하려면 신뢰된 조립 코드가 `content.metadata_source(token_loader, project, kind)`를 해당 `(project, kind)` 관찰자로 등록할 수 있습니다. 이 어댑터는 이미 보관된 bytes를 다시 검증하여 메타데이터 SHA·크기를 만들도록 합니다. 원래 관찰 시각을 보존하므로 오래된 QA 증거를 새 관찰로 승격하지 않습니다. 토큰 로더는 서버 등록 코드이며 HTTP 입력으로 선택하지 않습니다.
