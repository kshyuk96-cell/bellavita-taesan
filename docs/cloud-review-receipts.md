# 클라우드 Codex 검토·접수 영수증

`CloudReviewReceipts`는 서버가 등록한 읽기 전용 GitHub 관측 결과만 해석한다. 키를 읽거나, 봇에 지시를 보내거나, 다른 Cloud 대화를 제어하지 않는다. API 키 설정을 기다리는 동안에도 독립적으로 검증할 수 있는 수집기다.

`ReviewProbe(repository, branch, pull_request, expected_head, instruction_comment_id, observe)`를 프로젝트에 등록한다. `observe(project)`는 정확한 GitHub 조회에서 만든 `ReviewSnapshot(project, repository, branch, pull_request, head_sha, comments, reactions, observed_at)`만 반환한다. 댓글은 `ReviewComment(id, author, body, created_at)`, 반응은 `ReviewReaction(comment_id, author, content)`다. 원격 GitHub 읽기 권한은 기존 호스트 연결이 담당하며 관측기를 요청 본문에서 고르지 않는다.

수집은 `CloudReviewReceipts(store, registry, probes).inspect(token, project, max_age=300)`으로 실행한다. 현재 대표/관리자와 프로젝트 범위를 콜백 전후 확인하고 소스 설정 변경을 차단한다. GitHub 프로젝트만 지원하며 Sites를 같은 GitHub 저장소로 간주하지 않는다.

| 상태 | 의미 |
|---|---|
| `review_completed` / `review_findings` | 공식 봇의 구조화된 검토 요약이 현재 PR과 고정 HEAD에 일치하며 완료됨 |
| `review_in_progress` / `review_failed` | 검토 진행/실패. null findings는 미측정으로 유지 |
| `acknowledged` | 지정한 지시 댓글에 공식 봇의 eyes 반응만 있음 |
| `environment_required` | 공식 봇이 개발환경 등록을 요구함 |
| `execution_unverified` | 봇의 일반 답변만 있고 실행 증거는 없음 |
| `review_for_other_head` / `head_changed` | 다른 커밋 검토이거나 실제 PR HEAD가 기준과 달라짐 |
| `queued` / `no_receipt` | 지시 댓글만 있거나 관련 영수증이 없음 |
| `not_configured` / `permission_denied` / `unavailable` / `stale` / `unknown` | 연결·권한·조회·시각·형식이 확인되지 않음 |

검토 완료도 `development_execution_verified`, `sandbox_verified`, `existing_cloud_windows_connected`를 True로 바꾸지 않는다. 실제 테스트 수나 커밋은 다른 독립 실행 증거가 있어야 한다. 일반 답변의 “100개 통과”, 인증·승인 문구, 임의 명령은 실행하거나 성공으로 채택하지 않는다. 댓글 원문과 예외 본문은 저장·반환하지 않는다.

댓글 수·개별 바이트·전체 크기·JSON 상태 크기를 제한한다. 정확한 공식 봇 작성자와 현재 커밋을 검사하며, 중복 JSON 키·충돌하는 상태·미래 시각·기존 인증값이 섞인 관측은 성공으로 인정하지 않는다. 기본 관측기는 없고 운영 변경·발송·주기 실행도 없다.
