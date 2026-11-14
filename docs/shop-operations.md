# 쇼핑몰 첫 운영판 확인 절차

이 변경은 PR #20 / `codex/shop-backend` 전용이다. main 병합은 Render 자동 배포를
일으킬 수 있다. 운영 병합·실결제 활성화는 별도 배포 요청 이후 진행한다.

## 관리자와 고객 주문

등록된 대표 카카오 계정에서 `쇼핑몰 관리` → `관리 화면 열기`를 누른다.
서명된 링크는 10분 동안 유효하다. 만료되면 같은 명령으로 다시 연다.
상품 수정은 새 판매 링크에 적용되며, 기존 판매 링크의 상품·옵션·가격·행사는
바뀌지 않는다. 각 주문에도 상품 스냅샷과 실제 입력 내용이 저장된다.

행사별 링크는 `/shop/order/{offer_id}`, 주문번호는 32자리 식별자다.
원결제금액은 서버가 판매 링크와 선택 옵션으로 계산한다. 부분취소는 원결제금액과
분리하여 거래별 금액·합계·잔액을 표시한다. 전체 승인취소는 원금 전체로 표시한다.
이 변경 이전 취소 내역에 금액 증거가 없으면 확인 필요로 표시한다.
쇼핑몰 주문은 초대장 주문 내역과 분리한다. 취소 실행은 페이앱 판매자 화면에서 한다.

## 결제 확인 필요 주문

1. 관리 화면 주문 내역의 `결제 요청 확인 필요` 주문을 먼저 확인한다.
2. 페이앱 판매자 결제현황에서 연동 변수 `var1`의 주문번호를 찾는다. 표시되지 않으면
   접수 시각·전화번호·원결제금액·상품명으로 후보를 찾고, 가맹점 지원에 주문번호로
   원거래 대조를 요청한다. 같은 전화·금액의 주문이 여러 개면 추측해서 연결하지 않는다.
3. 원결제번호, 원금액, 전화번호, 확인 상태와 대조 근거를 관리자 양식에 기록한다.
   금액·전화번호 불일치, 다른 주문에 연결된 결제번호는 거절된다.
4. 페이앱에 기존 거래의 결제통보 재전송을 요청한다. 통보 주소는
   `https://<검증환경>/invitation-orders/payapp`이며 리디렉션 없이 `SUCCESS`를 반환해야 한다.
   대조 기록은 감사 이력이며, 이 기록만으로 결제 상태가 완료로 바뀌지 않는다.
5. 통보 이후 동일 주문번호에 결제번호와 상태가 반영됐는지 확인한다.
   미확인 상태에서는 새 결제 요청을 만들거나 고객에게 다시 결제하도록 안내하지 않는다.
   결제 요청이 없거나 취소됐다는 사실을 가맹점에서 확인한 뒤에만 별도 주문을 안내한다.

타임아웃, 잘못된 응답, 잘못된 결제 URL은 `requesting`으로 남는다. 동일 주문번호로
자동 재요청하지 않는다. 명확한 API 거절은 `failed`로 남으며 같은 주문번호로 재요청하지 않는다.
대기 통보가 먼저 와서 결제번호만 확인된 경우에도 결제 URL을 임의로 만들지 않는다.

## 페이앱 통보 계약

[공식 페이앱 개발자센터](https://api.payapp.kr/dev_center/dev_center01.html)와
[공식 통보 처리 예제](https://docs.payapp.kr/dev_center01.html)를 2026-11-13에 대조했다.
`userid`, `linkkey`, `linkval`, `responsedate`, SHA-256 해시를 검사한다. 해시는
문서에 따라 대소문자를 허용한다. 원결제번호와 서버 금액을 반드시 대조한다.
재전송을 허용하기 위해 오래된 응답 날짜 자체를 거절하지 않고 거래별 중복을 제거한다.
통보 전문의 인증값·카드정보는 저장하지 않는다.

| 상태 | 처리 |
| --- | --- |
| 1 / 10 | 요청·대기. 기존 결제·취소 상태를 되돌리지 않음 |
| 4 | 승인. 부분취소·승인취소보다 늦어도 되돌리지 않음 |
| 8 / 32 | 미결제 요청 취소. 이미 승인된 주문은 유지 |
| 9 / 64 | 전체 승인취소 |
| 70 / 71 | `orig_mul_no`, `orig_price`로 원거래 검증. `mul_no`별 `price`를 부분취소액으로 저장 |

부분취소의 동일 `mul_no` 재전송과 70/71 전환은 한 건으로 집계한다. 금액이 달라진
중복이나 원금 초과 합계는 거절한다. 기존 이력 없는 취소 주문은 승인으로 복원하지 않는다.
가맹점에서 실제 부분취소 번호·금액 형식을 추가 확인한 뒤 운영 활성화한다.

## 영구 디스크, 백업과 복구

운영 Render 서비스에서 다음을 **읽기 전용으로** 확인한다. 서비스 중지나 운영 데이터
이전은 배포 요청 범위에서 진행한다. 기본값 `taesan-data`를 영구 저장으로 간주하지 않는다.

```sh
python manage_data.py inspect --source "${TAESAN_DATA:-taesan-data}"
```

`data_path`가 실제 디스크 마운트 아래이고, `/var/data` 사용 시 `under_var_data`와
`var_data_is_mount`가 모두 true여야 한다. Render 서비스의 디스크 마운트 경로도 대조한다.
단일 인스턴스를 유지한다. DB·주문 수·초대장 파일을 기록하고 검증환경 재배포 후 재확인한다.

백업은 고객 주문·콜백·직원 업무를 포함해 모든 쓰기를 멈춘 상태에서 진행한다.
정지 시간 중 페이앱 통보는 이후 재전송되도록 준비한다. 백업에는 직원 인증과 연락처도
포함되므로 운영 서비스 외부의 접근 제한 저장소에 보관한다. 같은 디스크만으로는
디스크 장애 복구가 되지 않는다.

```sh
python manage_data.py backup --source /var/data/taesan-data \
  --destination /secure-backups/taesan-before-migration --service-stopped
python manage_data.py restore --source /secure-backups/taesan-before-migration \
  --destination /var/data/taesan-restored --service-stopped
python manage_data.py inspect --source /var/data/taesan-restored
```

도구는 SQLite 백업 API와 무결성 검사를 사용하고 초대장·관련 파일을 함께 복사한다.
복구 시 파일 목록과 SHA-256을 검사하며 기존 디렉터리를 덮어쓰지 않는다.
복구 후에만 `TAESAN_DATA`를 새 경로로 지정한다. 원본은 보존한다. DB 내부 헤더는
백업 시 달라질 수 있어 원본과는 SQL 데이터, 백업본과 복구본은 체크섬으로 대조한다.

## 독립 검증환경 재현

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
openssl req -x509 -newkey rsa:2048 -nodes -keyout /tmp/bellavita-test.key \
  -out /tmp/bellavita-test.crt -days 2 -subj /CN=localhost -addext subjectAltName=DNS:localhost
python tests/serve_shop.py --data /tmp/bellavita-shop-demo \
  --cert /tmp/bellavita-test.crt --key /tmp/bellavita-test.key
```

별도 터미널에서 Playwright Python 패키지와 Chromium이 준비된 환경으로
`python tests/browser_shop.py`를 실행한다. 데모는 로컬 HTTPS 443 포트를 사용한다.
`/tmp/bellavita-shop-demo/test-links.json`에 관리자·두 가격 링크가 생성된다.
데모의 사진은 로컬 쌀 중량 이미지로 대체한 합성 자료이며 실제 예식 사진이 아니다.
모든 결제 요청은 서버 내부 모의 함수로 대체되고, 실제 페이앱·시트·알림·모델을 호출하지 않는다.
관리자 링크 갱신은 데모 재시작으로 가능하다. 주문과 판매 링크는 같은 데이터 경로에서 유지된다.

실가맹점 검증은 별도 HTTPS 검증 서비스에 전용 테스트 가맹점 정보가 준비된 후 진행한다.
가맹점의 테스트 수단 제공 여부, 결제 완료·전체취소·부분취소와 통보 재전송 지원을 확인한다.
테스트 가맹점이 없으면 사용자가 실거래 금액과 취소를 승인하고 직접 실행해야 한다.
자격증명은 환경 설정에만 등록한다. 이 작업에서 운영 결제와 원장 쓰기는 활성화하지 않는다.

## 후속 제작·원장 연결

동일한 주문번호를 제작 접수·주문 원장·배송에서 사용한다. 통보 수신 자체로 제작을 호출하지
말고, 결제 확인 후 `order_id`에 UNIQUE 제약을 둔 제작 접수/outbox를 한 트랜잭션에서 만든다.
재통보·부분취소·승인취소는 제작을 재접수하지 않는다. 사진 검토 및 취소 시 제작 중지 정책은
후속 단계에서 연결하고, 운영 시트 쓰기는 사본에서 검증한 이후에만 켠다.
