# 자체 주문 알림 연결

새로 결제 완료된 주문은 직원 확인 전 상태로 보관하고 고객·직원 알림을 각각 발송합니다. 과거 주문은 소급 발송하지 않습니다. 발송 제공사의 접수 성공과 고객 휴대폰 수신 성공은 구분합니다.

## 센드온

공식 문서 확인일: 2026-11-20.

1. 개인회원이면 센드온 마이페이지에서 사업자 전환 후 사업자 인증을 완료합니다. 공식 문서상 API는 사업자 인증 회원만 사용할 수 있습니다.
2. 발신번호를 등록하고 인증합니다.
3. 마이페이지 → API KEY → 새로운 API KEY 생성으로 키를 발급합니다. 계정당 한 개를 지원합니다.
4. API KEY 관리 → 허용 IP 관리에 **실제 발송 서버**의 공인 IP 범위를 등록합니다. 등록이 없으면 모든 API 호출이 차단됩니다. 해외 서버도 별도 승인 없이 등록할 수 있습니다.
5. Render에서 서비스 상세 페이지 → Connect → Outbound에 표시되는 모든 CIDR 범위를 사용합니다. 작업용 컨테이너의 IP나 개인 PC IP를 넣지 않습니다.
6. 다음 값을 Render 비밀 환경변수로 저장합니다. API 키를 채팅 본문이나 소스에 넣지 않습니다.

```text
INVITATION_SMS_PROVIDER=sendon
SENDON_ID=<센드온 계정 ID>
SENDON_API_KEY=<발급한 API KEY>
SENDON_FROM=<등록·인증한 발신번호>
SENDON_ALIM_PROFILE_ID=<승인된 카카오 채널 ID>
SENDON_CUSTOMER_ALIM_TEMPLATE_ID=<승인된 고객 주문확인 템플릿 ID>
SENDON_STAFF_ALIM_TEMPLATE_ID=<승인된 직원 주문승인대기 템플릿 ID>
INVITATION_STAFF_NOTIFY_PHONES=<직원 수신번호, 쉼표 구분>
INVITATION_ORDER_SMS_ENABLED=1
```

센드온의 포인트/크레딧 잔액과 문자 발송 후 승인 정책도 계정주가 확인해야 합니다. 현재 어댑터는 포인트를 사용하며 `useCredit=false`입니다. API 발송 후 승인 메뉴는 콘솔 → 발신번호 → 발송 후 승인입니다.

직원과 고객 알림 모두 `POST https://api.sendon.io/v2/messages/kakao/alim-talk`을 사용하며 `Authorization: Basic base64(ID:APIKEY)`를 사용합니다. 정상 발송은 알림톡으로 처리하고, 실패한 경우에만 같은 내용의 정보성 LMS로 대체 발송하며 `isAd=false`입니다. HTTP 200이더라도 응답 본문 `code`가 200이고 `data.groupId`가 있어야 제공사 접수로 기록합니다.

고객·직원 알림톡 템플릿에는 `#{주문번호}`, `#{행사명}`, `#{금액}`, `#{행사일시}`, `#{장소}`, `#{주문확인링크}` 변수를 정확히 등록하고 사전 승인을 받아야 합니다. 고객 템플릿의 링크는 주문 확인 페이지, 직원 템플릿의 링크는 승인 대기 주문 페이지로 연결됩니다.

## 운영 확인

- 고객 메시지는 결제 확인 및 직원 검토 중임을 안내하고 해당 주문의 영수증 링크를 포함합니다.
- 직원 메시지는 승인 대기 안내와 직원 주문 확인 링크를 포함합니다.
- 중복 결제 콜백은 같은 알림을 다시 등록하지 않습니다.
- 네트워크 오류나 결과 불명확 건은 자동 재발송하지 않습니다. 제공사의 발송 이력을 확인한 후 별도 조치해야 합니다.
- `accepted`는 제공사 접수이며 `delivered`를 의미하지 않습니다. 단말 수신 확인은 아직 연동하지 않았습니다.
- 결제 취소 건과 72시간 지난 대기 알림은 발송하지 않습니다.
- 아직 API 키·허용 IP·인증 발신번호·잔액을 연결하지 않은 상태에서 실발송 성공을 주장하지 않습니다.

## 공식 출처

- [센드온 API KEY 발급](https://sdk.sendon.io/reference/1-api-key-발급받기)
- [센드온 인증 헤더](https://sdk.sendon.io/reference/2-기본-설정하기)
- [센드온 API 보안·허용 IP·사업자 인증](https://sdk.sendon.io/reference/3-연동-전-보안-정책-확인하기)
- [센드온 문자 발송](https://sdk.sendon.io/reference/msgcontroller_send)
- [센드온 응답 코드](https://sdk.sendon.io/reference/4-api-응답-코드-가이드)
- [Render 발송 서버의 IP 범위 확인](https://render.com/docs/outbound-ip-addresses)
