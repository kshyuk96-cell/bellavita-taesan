# ERP 초대장 자체 주문서 검수

기준: `codex/shop-backend`의 automatic-invitation-scroll-development-brief.md (0cac58ee5decad52f41718e99060b75421f62afb). 공개 쇼핑몰 4종 상품 코드를 가져오지 않고 기존 초대장 주문 경로만 확장했다.

- ERP `orderMode=invitation-checkout`은 행사명과 일치하는 기존 93,500원·99,000원 상품을 모두 확인한다. 복제 원본은 필요 없지만 행사 상품 등록은 필요하다. 상품 생성·가격 변경·실가맹점 호출은 수행하지 않는다.
- 기존 `blogpay-direct`, /jobs, 카카오 경로와 이미 발행한 링크는 유지한다. ERP의 INVITATION_ORDER_MODE를 설정하지 않으면 기존 방식이다.
- 미리보기·발행의 행사/버전별 공개 ID를 예약한다. 상품 옵션·사진·선택한 크롭까지 지문으로 확인하고, 발행 시 재검증한다. 두 가격 주문 스냅샷과 고객 페이지 manifest가 모두 있어야 주문서를 읽는다.
- 고정된 snapshot은 기존 invitations/{job}/{price}.order.json, 고객 개인정보와 주문의 전체 상품 snapshot은 기존 state.sqlite3의 invitation_orders에 저장한다. 공개 경로로 주문자 정보를 보내지 않는다.
- 직원 전용 /erp/invitations/jobs/{requestId}/order-preview/{price}는 Bearer 인증이 필요하며 결제를 비활성화한다.
- 결제 생성의 불명확한 응답은 requesting 상태로 유지해 같은 attempt의 재호출이 두 번째 결제를 만들지 않는다. 서버가 가격·옵션을 재계산한다. 서명 알림만 결제 상태를 바꾼다.
- 취소된 미처리 업무는 payment_hold로 보류하고 주문 인계 직전 결제 상태를 재검사한다. payment_events 원장은 같은 부분 취소의 70/71 알림을 한 건으로 처리하고, 금액 충돌·총액 초과·잘못된 참조·이전/이후에 도착한 알림을 검증한다.

## 재현

```sh
python -m unittest discover -s tests
python scripts/generate_checkout_review.py
python -m http.server 4173 --bind 127.0.0.1 --directory work/checkout-demo
# 별도 개발 환경에 @playwright/test 설치 후:
node scripts/verify_checkout_review.mjs
```

결제 비활성 샘플 4개: http://127.0.0.1:4173/event-93500.html, /event-99000.html, /wedding-93500.html, /wedding-99000.html. 인물은 실제 사진 대신 가상 사진 영역으로 검수한다. 실제 인물 사진의 얼굴 잘림은 별도 확인이 필요하다.

검증: Python 58건 통과. Chromium 375/390/430/1365px에서 4개 주문서, 금액·옵션·동일 주문자·쌀 이미지·HTML 문자·가로 넘침·결제 차단 80개 assertion 통과. 실제 휴대폰 카카오 공유/실가맹점 결제/실취소는 미검증. SQLite backup API로 별도 DB 복원 후 snapshot과 중복 방지 원장이 유지됨을 검증했다.

운영 병합·배포·INVITATION_CHECKOUT_ENABLED 활성화·실결제·실발송은 하지 않았다. 별도 승인 및 행사별 상품 등록/가맹점 설정 대조가 필요하다. 알림톡 공급사·승인 템플릿·인쇄용 실제 PNG/PDF 제작은 이번 변경으로 완료 처리하지 않는다. SMS/MMS 대체 전송을 추가하지 않았다.
