# 서버 저장 샘플·부분취소 재개 검증 (2026-11-14)

## 확인 링크

- [샘플 고객 주문서 · 93,500원](https://bellavita-shop-sample.shrewdmaxim1.chatgpt.site/?product=wedding-sunflower&offer=0)
- [샘플 고객 주문서 · 99,000원](https://bellavita-shop-sample.shrewdmaxim1.chatgpt.site/?product=wedding-sunflower&offer=1)
- [샘플 관리자](https://bellavita-shop-sample.shrewdmaxim1.chatgpt.site/?view=admin)
- [재배포 보존 검증 주문](https://bellavita-shop-sample.shrewdmaxim1.chatgpt.site/?order=BV-7303aba8-3dab-4bf9-a446-47e28c1db60a)
- [PR #20](https://github.com/kshyuk96-cell/bellavita-taesan/pull/20)

기존 Site ID와 소유자 전용 접근 범위를 유지했다. 샘플 기록은 Python 운영 DB와 분리된
Site D1 저장소에 보관하며 외부 발송·결제·영수증 API를 호출하지 않는다.
실제 Python 관리자 진입은 기존 대표 카톡 명령 `쇼핑몰 관리`의 10분 유효 링크를 사용한다.
부분취소 주문을 펼치면 `부분취소 잔액 대조` 양식이 표시된다.

## 실행한 검증

- Site: Node 상태·SQLite 기반 D1 API·JSDOM UI 테스트 4개 통과.
- 호스팅 API: 5상품 조회 → 테스트 주문 생성 → 동일 접수 재요청 1건 유지 → 독립 요청 재조회 →
  모의 결제 → 10,000원 부분취소 → 83,500원 잔액 대조·재개 → 배송 중 →
  저장된 문구 이미지 응답 → 추가 부분취소로 승인 해제 → 모의 전체취소.
- 다음 버전을 같은 Site에 배포한 뒤 기존 주문번호·문구·배송 중 단계·취소 상태·7건 이력·이미지 보존 확인.
- Python: 최신 main 포함 전체 `python -m unittest discover -s tests -q` 84개 통과.
  서버 계산·기존 초대장/직원 기능·중복/역순 통보·취소·영수증·알림·수신기 회귀 포함.
- 부분취소 전용: 틀린 잔액/오래된 취소 버전/오래된 관리자 화면 거절, 동일 재개 중복 방지,
  새 취소로 재개 승인 해제, 수신 중 취소, 늦은 재개 이벤트가 수동 보류를 해제하지 않음,
  사진 검토 전 재개 차단과 부분취소 후 사진 검토, 동일 주문 원장·제작 행 1개 유지.

Python 관리자 화면은 Chromium 375px에서 대조 저장 → 배송 중 변경 → 추가 취소 후 배송 차단을 검증했다.
가로 넘침과 JavaScript 오류가 없었고 대조 이력의 백업·복구도 통과했다.

재현 가능한 Python 검증: `tests/test_partial_reviews.py`, `tests/browser_partial_reviews.py`.
Python 검증 코드는 `partial_reviews.py`와 해당 테스트를 포함한 PR HEAD에서 식별한다.
Site 최종 소스 커밋: `c8ff75c8455e29ac3e1ea776c24fa474a72d11af`.
Site 배포: `appgdep_6af7f4d024ac8191941be9ca1f093b2f` (succeeded).

## 예외와 해결

- 로컬 소켓 제한은 별도 허용된 실행으로 격리 HTTP 테스트를 실행해 해결했다.
- 관리자 검증 오류는 기존 규격대로 HTTP 422다. 테스트의 잘못된 400 기대값을 수정해 재실행했다.
- Site 저장소 선언의 잘못된 객체 형식은 배포 검증에서 거절됐다. 공식 런타임이 요구한 문자열
  `"d1": "DB"`로 수정한 뒤 같은 Site에 정상 배포했다.
- 만료된 Site 단기 Git 인증은 새 자격으로 교체했다. 저장소·Site ID·공유 범위는 유지했다.
- 모바일 테스트 프록시에서 POST Origin/리다이렉트 처리 문제가 관찰됐다. 테스트 요청을
  격리 로컬 서버로만 전달하고 상태 재조회는 GET으로 수행하도록 수정했다.

## 검증 한계

Sites 브라우저 제어 도구가 없어 해당 Site의 실제 브라우저 렌더링 검증은 수행하지 않았다.
JSDOM 기능 검증과 실제 호스팅 API·재배포 보존 검증을 구분한다.
Render 운영 재배포, 실가맹점 결제·취소, 단말 MMS/카카오 수신, 국세청 반영,
실제 출력·운영 Sheets 쓰기는 수행하지 않았다.
