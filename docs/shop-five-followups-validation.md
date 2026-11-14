# 승인한 다섯 후속 작업 검증 (2026-11-14)

기존 PR #20, `codex/shop-backend` → `main`. 최신 main
`9348afabf049322b2bf3f061d511f5848d7aa1bf`를 PR 브랜치에 통합했다.
운영 main 병합·Render 운영 배포는 하지 않았다.

## 구현·검증

| 승인 항목 | 결과 |
| --- | --- |
| 알림톡 어댑터 | 공식 SOLAPI ATA 규격, 승인 템플릿/변수/문구 샘플 링크, 직원·주문자 알림, SMS 대체 꺼짐, 요청/채널 고정. 모의 접수·수신·실패·타임아웃·결과 불명 재요청 차단 검증. |
| 제작 PNG·PDF | 주문번호·문구·보내는 분·쌀 중량 반영, 규격·DPI 지정, 출력 경계·PDF MediaBox 검증. 5상품의 기본 문구 예시 10파일 생성. |
| 검수 화면 | 신규 주문 검수 전 공장/배송 대기, 검수자·근거·시각 기록, 새 버전 재승인, 파일 유실/변조 차단. 수신기는 승인한 두 파일을 검증한 뒤 동일 주문 행으로 ACK. |
| 통합 현황판 | 최근 쇼핑몰 주문 200건의 결제 확인·제작 보류·배송·알림 실패·영수증 대조, 주문번호/문구/보내는 분 검색, 최근 자동 백업 상태. |
| 백업 자동화 | 실제 60초 주기로 두 번의 스냅샷과 자동 복구 시험 완료. SQL·이미지/PDF·이력 보존, 동시 실행 1회, 변조/불안정 백업 실패 처리와 이전 정상본 보존 검증. |

- 전체 `python -m unittest discover -s tests -q`: 99개 통과.
- 신규 시나리오 `tests/test_shop_followup.py`: 13개. 기존 주문/결제/초대장 회귀와 구분한다.
- `.venv/bin/python tests/browser_shop_followup.py`: Chromium 375px에서 현황판 필터·검색·파일 생성·검수·승인 파일 응답 검증.
  가로 넘침과 JavaScript 오류 없음. [결과](validation/shop-followup-mobile.json).
- `python tests/verify_scheduled_backup_timer.py`: 실제 60초 간격, 정상 서비스 DB 쓰기 사이에서
  스냅샷 2회·자동 복구 시험 2회. [결과](validation/shop-followup-backup-timer.json).

## 사용자가 확인할 샘플

- [샘플 관리자·현황판](https://bellavita-shop-sample.shrewdmaxim1.chatgpt.site/?view=admin)
- [93,500원 주문서](https://bellavita-shop-sample.shrewdmaxim1.chatgpt.site/?product=wedding-sunflower&offer=0)
- [99,000원 주문서](https://bellavita-shop-sample.shrewdmaxim1.chatgpt.site/?product=wedding-sunflower&offer=1)
- [기본 문구 PNG 예시](https://bellavita-shop-sample.shrewdmaxim1.chatgpt.site/examples/wedding-sunflower.png)
- [기본 문구 PDF 예시](https://bellavita-shop-sample.shrewdmaxim1.chatgpt.site/examples/wedding-sunflower.pdf)

Site는 기존 소유자 전용 접근과 기존 기록을 유지한다. 새 주문에서 모의 결제 → 검수 샘플 생성 →
검수 승인 → 배송을 체험할 수 있다. 알림톡 결과 불명 → 대조 후 실패 확인 → 재요청도 모의 흐름이다.
Site의 검수 이미지는 SVG 체험본이며, 다운로드 PNG/PDF는 상품별 기본 문구의 고정 예시다.
실제 주문별 PNG/PDF 생성·파일 검증·자동 백업 엔진은 PR의 Python 백엔드에 구현했다.
운영 백엔드와 Site 저장소는 별개다. Site 자체의 자동 백업 활성화로 표시하지 않는다.

Site 소스 `28ad17790b6fbba4092e1c2243051ca6fcb7f6d3`, 배포
`appgdep_6af7fa7c3bc4819186a4232c6cf16c15` 성공.
Site Node/JSDOM 테스트 5개가 통과했다. 호스팅 API에서도 기존 기록 보존, 새 검수·배송, 결과 불명 재요청 차단과 출력 예시 10파일 해시 일치를 확인했다.
사이트는 Node/JSDOM과 호스팅 API로 검증하며, Sites 브라우저 제어 도구 부재로 실제 Site 렌더링은 미검증이다.
Python 모바일 브라우저 검증과 구분한다.

## 발견 오류와 해결

- 신규 설정 분기에 `os` import가 빠져 주문 요청이 실패했다. import를 추가한 뒤 전체 회귀를 재실행했다.
- 승인 파일 새 버전을 기존 `ready` 이벤트로 받으면 수신기의 보류 해제가 누락됐다.
  승인된 artifact가 있는 새 버전만 재개하도록 보완하고 새 버전/오래된 이벤트 시나리오를 다시 검증했다.
- 파일 유실 시 읽기 예외는 관리자에게 백업 복구 안내와 HTTP 422를 반환하도록 수정했다.
- Site의 기존 샘플 요소와 새 검수 변수 이름 충돌을 JSDOM이 발견했다. 이름을 분리해 재검증했다.
- 타이머 검증 스크립트의 쓰기 대상 컬럼을 실제 판매 링크 테이블로 수정하고 60초 간격 시험을 재실행했다.

실제 가맹점 결제/취소·국세청 반영·카카오/MMS 단말 수신·실제 출력·운영 Sheets 쓰기는 수행하지 않았다.
운영 배포와 실제 발송 활성화는 별도 요청 범위다. 설정 완료 여부를 성공한 실거래로 간주하지 않는다.
