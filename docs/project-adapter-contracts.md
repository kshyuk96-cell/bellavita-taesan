# 고객 프로젝트 업무 어댑터 개발 계약

아직 운영 공급자를 등록하지 않은 개발 코드다. `ProjectAdapters`에는 임의 URL/SQL을 받는 기능이 없으며, 서버가 미리 등록한 `ProjectProvider` 콜백만 호출한다. 대표/관리자 토큰과 고객 애플리케이션의 직원 인증은 별개다. 현재 모의 공급자 시험만 수행했고 실제 결제·취소·예약·부고 수정·시트 쓰기는 실행하지 않았다.

## 소스 근거

| 프로젝트 / 고정 소스 | 읽기 동작 | 실제 근거 |
|---|---|---|
| 벨라비타 ERP `2c10b03db14c607fb4c61e09cbdf52ec7b406f40` | `orders.list` | `worker/api.js`의 GET `/api/orders`: `orders[]`에 `id,date,time,status,revision,stableUid,sourceRow,amount,paid` 등. 상태는 접수/제작완료/배송완료/미확인. 금액 null은 미확인, 0은 실제 0을 유지한다. |
| 벨라비타 쇼핑몰 `d5047f3345b757edfcfbd81a73c609c0392007dd` | `orders.list`, `order.event` | `shop_admin_queries.py`와 `checkout.py`: `invitation_orders.state/details`, `payment_events.event_key` 및 취소 상태 우선 규칙. 세부 주문의 연락처는 응답에서 제외한다. |
| 희망일출 `d4da64b3791bb8dd7a96cf2f974c784385e79687` | `products.list`, `inquiries.list` | `src/backend.mjs` GET `/api/bootstrap`의 `products[]`, `requests[]`; `src/operations.mjs`의 업무 단계 및 기존 status 변환. 견적·인원 미확인/null을 임의의 확정값으로 바꾸지 않는다. |
| 장례지도사협회 `8f1e4fa331dbf460f0fcbea432c0fa360cc1b4ba` | `access.status`, `obituary.get` | `lib/auth.ts` currentUser, `lib/obituaries.ts` authorize/getObituary, `lib/types.ts`. 같은 장례식장이라는 사실만으로 접근을 허용하지 않는다. |

`ProjectProvider`는 프로젝트·회사·소스·브랜치·검토 커밋을 지정한다. 현재 공급자 소스와 관측된 응답 소스 SHA가 고정 계약과 일치해야 한다. 소스가 진전되면 새 계약 검토와 테스트가 필요하다. 설정은 읽기 전후 및 로컬 이벤트 기록 전 다시 확인한다. 응답은 5분 이내 관측만 허용하며 미래 시각도 미확인으로 처리한다.

ERP와 희망일출의 현재 구현은 소유자 전용 Sites 접근을 전제로 한다. 이 어댑터 추가만으로 직원 역할 인증이 생기지 않는다. 실제 연결 때는 전용 서비스/사용자 경계와 최소 권한을 먼저 정해야 한다.

## 부고 접근

협회 공급자는 서버에 묶인 `bound_subject`의 실제 현재 세션을 확인한다. `obituary.get`에서는 원본 `authorize(user,id)`와 동등한 검사를 실제 실행한 결과를 정확한 사용자/부고 ID에 결합해야 한다. staff는 원본의 활성 사용자, 같은 장례식장, 해당 사건 담당자 또는 운영자 조건을 충족해야 하며, mourner는 해당 부고의 유효 접근권이 필요하다. expert는 부고 조회 권한으로 간주하지 않는다.

모의 시험의 `authorization_verified`는 공급자 콜백이 생성하는 증거 필드이며 HTTP 요청이나 모델이 지정하는 승인값이 아니다. 실제 인증 공급자는 아직 연결하지 않았다. 부고 응답에는 상주 연락처/계좌/고인 개인정보를 포함하지 않으며, closed는 종료 상태와 ID만 반환한다.

## 결제 이벤트는 로컬 검토 기록

`sync_event(token,project,order_id,event_key)`는 참조 두 개만 받는다. 서버 공급자가 원본에서 이벤트를 검증하고 현재 주문 스냅샷과 함께 다음 개발용 봉투를 생성해야 한다.

```
{order_id, event_key, revision, state, source_verified: true}
```

`revision`은 미래 소스 어댑터가 영구적으로 관리할 단조 증가 스냅샷 번호다. 현재 PayApp가 제공하는 값이 아니며 원본 payment_events에 이 번호가 있다고 가정하지 않는다. 실제 공급자에는 이 번호를 생성·보존하는 증분 어댑터가 추가로 필요하다. 원본 PayApp 서명 검증을 대체하지 않는다.

동일 이벤트의 동일 스냅샷은 duplicate, 내용 변경은 conflict로 처리한다. 낮은 revision은 stale이고 같은 revision의 다른 이벤트나 취소 이후 paid 도착은 needs_reconciliation이다. 이 때 로컬 최신 검토 상태를 자동 덮어쓰지 않는다. 원본 코드의 request_cancel 후 재승인 같은 예외는 이 경로에서 임의 확정하지 않고 원본 재조회 대상으로 남긴다. 실제 결제/환불/주문 상태 변경 함수는 호출하지 않는다.

## 결과 구분

연결 없음 `not_configured`, 통신 실패 `unavailable`, 공급자 권한 부족 `permission_denied`, 누락/중복/불명확/오래된 근거 `ambiguous`, 실제 빈 목록 `empty`를 구별한다. 목록은 최대 100건까지 반환하고 잘렸는지와 매칭 건수를 함께 표시한다. 외부 본문은 업무 자료로만 반환하고 지시 권한으로 사용하지 않는다. 원본 응답·PII를 로컬 이벤트 DB에 저장하지 않는다. 알려진 관리 bearer가 입력/응답/receipt에 섞이면 해당 내용을 거부 또는 가림 처리한다.
