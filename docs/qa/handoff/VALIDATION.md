# 검증 기록 — 세 입력 기반 고객 인계 검증 접수

- 이번 요청은 기존101–120 개발보다 URL·테스트 계정·개발 Codex 목록 기반의 고객 인계 검증 흐름을 우선하는 변경으로 처리했다.
- `python -m unittest discover -s tests -q`: **1,781 실행 / 1,780 통과 / 1 skip / 실패·오류0**, 146.309초. 로그 `work/taesan-handoff-release-regression.log`는 private 작업 공간에 있다.
- `python -m unittest tests.test_handoff_intake -q`: **13개 통과**. 전체와 합산하지 않는다. 전체 회귀 후 잔여 영향/보완 승인계획의 설명을 결과 비고에 보존하는 표시 보강을 했고 집중13개를 다시 통과했다.
- 개발자 PASS를 독립 통과로 채택하지 않음, 결제 P3 표기를 P0로 산정, QA 추가 계획은 실행 수와 분리, TXT/Markdown/JSON/DOCX를 데이터로만 읽음, 코드블록 실행 없음.
- 명시되지 않은 테스트 항목을 조용히 누락/절단하지 않음. 과도한 개수·제목 길이·파일/XML 크기는 오류로 거절한다.
- 계정 username/password/otp는 메모리에서만 처리하고 문서에는 별칭·역할·접수/로그인확인 여부만 남긴다. 테스트 목록에 실수로 포함한 비밀 assignment도 가림. 알려진 비밀값·JSON 내 token·repr 미노출 확인.
- 독립 기대값의 구체 값·승인·근거 없으면 실행기의 PASS가 있어도 인계 보류. manager 계획의 중요도/필수 검사/시나리오 삭제·다른 source·검증되지 않은 source window·미실시 PASS 위조·미실시 추가 QA 검사·주요 결함·불충분한 CLOSED 재시험·시간/환경 누락을 차단.
- 등록 host verifier 인터페이스가 아니라 개발자의 JSON만 넣는 시도는 거절한다. CLI는 임의 evidence JSON으로 인계 판정을 만드는 옵션을 제공하지 않는다. 모든 일반 사이트의 실행 adapter가 이미 연결되었다는 의미는 아니다.
- 신뢰된 단위시험 fixture에서 조건 충족 시 고객 검토용 인계만 권고하고, 발주사 최종 인수·운영 승인은 false를 유지한다. 이 fixture의 성공을 실제 웹서비스 테스트 성공이라고 주장하지 않는다.
- Word/Excel 재열기, 수식으로 시작하는 외부 제목 escape, 실제 금액0을 미확인으로 바꾸지 않는 표시 확인.
- 읽기 URL probe는 unauthenticated GET 한 번. 로컬 HTTP 시험 서버에서 redirect를 따라가지 않음·Authorization/Cookie/POST 없음 확인. private/link-local/DNS 혼합 사설주소 거절. 응답 hash/HTTP200을 source 버전·UI 성공으로 채택하지 않음.

## 고객 포맷 예시

다음은 전달 형식만 확인한 **미실시 예시**다. 실제 개발자의 이번 제출 목록이나 새로 받은 테스트 계정이 아니다.

```text
python scripts/prepare_handoff_review.py \
 --url https://hope-tour-feedback.shrewdmaxim1.chatgpt.site/review/ \
 --project 희망투어클럽 --template hope-tour \
 --checklist docs/qa/handoff/developer-checklist.example.md \
 --output docs/qa/handoff/example
```

- 예시 개발자 주장3개, 고객 기준38행 유지, 독립 추가 검사 계획은 별도.
- 실제 실행0 / PASS0 / FAIL0 / BLOCKED0 / NOT RUN38. 문서 `DRAFT_NOT_VERIFIED`, `customer_ready=false` 및 완료 확인서 인계 불가를 표시한다.
- 실제 URL을 probe하거나 로그인·결제·form 제출·발송하지 않았다. 실제 source 버전·UI·서버·원장·PG·사용성 결과가 아니다.
- Word/Excel·JSON·보완요청서·SHA256SUMS 생성 및 재대조. 예시 파일의 계정 목록은 비어 있다.

## 실행 도구 제한

환경 `ccarenv_b64_Y2NhcmVudl8xZWM2OTQwYzYwMDQ4MTkxOWRhODRhMTFkYmQ2NjQ1Mw`는 running/connected이며 desktop 지원 enabled=true/state=unknown이다. 그러나 활성 도구·실행 경로 검색에서 실제 desktop JS/cua_repl.js/viewer 도구가 제공되지 않았다. [Cloud desktop SKILL.md](skill://plugin_connector_1p_c5b7d5df5d7081918f2c4be5a633ed5d/cloud-desktop/SKILL.md)의 missing-tool 진단과 Sites의 skip-browser 지침에 따라 화면 조작·preview/브라우저 설치·비공식 대체 제어를 시도하지 않았다. 이 환경에서 링크만으로 실제 UI 완료 확인서를 발행할 수 있다고 주장하지 않는다.

현재 제공물은 세 입력의 접수·계획·출력 포맷·최종 판정 guard다. 새 사이트의 실제 검증에는 지원 브라우저와 필요한 프로젝트별 읽기 증거 연결이 필요하다. 그 공백은 고객 확인서와 개발 Codex 보완 요청서에 남기며, 연결 전에는 인계 보류다. 운영 배포·실제 결제·발송·고객 자료 변경은 수행하지 않았다.
