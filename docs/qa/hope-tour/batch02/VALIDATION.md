# 검증 기록

- `python -m unittest discover -s tests -q`: 1,732개 실행, 1,731 통과, 1개 skip, 오류/실패0. 이 회귀 실행 후 독립 계산식 변조 테스트 1개를 추가했다.
- `python -m unittest tests.test_hope_batch02 -q`: 최종 추가8개 모두 통과. 위 회귀와 겹치므로 합산하지 않는다.
- 실제 서비스 버전33 고정 소스 Docker 실행: 개발항목20개 생성,19개 실행·단언,18개 일치,1개 실패,1개 미검증. 프로세스 종료0만으로 채택하지 않고 관리기 사전 계약으로 재판정했다.
- `scripts/run_hope_batch02.py`: 결과물 저장 후 의도대로 종료1. 인수 거절 사유 `critical_scenario_not_passed`, `required_coverage_missing`.
- 독립 계산식11개 일치. 테스트 원금 적립·취소 복원·반환액을 잘못 변경하는3가지 증거 변조를 검출. 실제 서비스 런타임에 주입한 결함이라고 표현하지 않는다.
- 관리기 보고서 expected/status 위조, 항목누락/중복/알수없는ID, 소스 불일치, 계약 P등급 하향, script 속 용어로 통과 위장, 브라우저 PASS 위장 거절 확인.
- Word·Excel 파일 재열기, 고객 시나리오38개 행 보존, 계산식 별도 시트, 실제소스21–40 시트 확인.
- 고객 결과: PASS0/FAIL1/BLOCKED13/NOT RUN24. 이것은 20개 개발항목 결과와 분모가 다르다.
- 운영 배포, 소스 Site push, 실제 PG·발송·운영 데이터 변경 없음.

재현 파일은 `run-20261116/`에 있다. `actual-source-evidence.json.gz`에는 실제 소스 실행의 원시 API trace와 테스트 원장·페이지가 있으며, 비밀키·세션 쿠키·비밀번호는 저장하지 않는다. 일부 원장 계정은 고정 합성 테스트 계정이다.
