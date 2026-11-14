# 대표 상품 4개와 실제 백엔드 통합

## 경로와 데이터

- 고객 전시: `/shop`. 행사·신랑신부 이름별 초대장 링크는 전시하지 않는다.
- 상품 주문: `/shop/products/{product_id}`. 설치 홀·주소·일시, 원본 상품 옵션, 모바일청첩장 요청 정보를 받는다.
- 관리자: 대표의 카카오 `쇼핑몰 관리` 명령이 주는 기존 10분 서명 링크. `/shop` 바로가기, 상품·가격·옵션 묶음 편집, 전시/주문 가능 여부, 전체 주문 검색을 제공한다.
- 원본 상품 ID: 1454214, 1440817, 1452802, 1969605. 기본가 각각 110,000원. 웨딩 사진 3호 선택은 +55,000원, 합계 165,000원.
- 등록: `python seed_shop_catalog.py --data /실제/TAESAN_DATA`. 최초 4개만 INSERT OR IGNORE로 등록하고, 재실행·재배포는 관리자 변경을 덮어쓰지 않는다. 운영 데이터에는 이번에 실행하지 않았다.
- 기존 SQLite shop_products/shop_offers/invitation_orders를 사용한다. 신규 주문마다 고정한 판매 스냅샷을 만들며 서버가 옵션 금액을 계산한다.
- 상품 수정은 신규 접수에 적용하고 기존 주문/행사 링크/요청 결과 불명 주문은 원래 스냅샷을 보존한다. 같은 접수번호는 결제 재요청하지 않는다. 변경된 설치 정보·유효하지 않은 옵션·오래된 상품 버전은 거부한다.
- 상품 상세는 해당 상품의 원본 설명·이미지를 사용한다. 기존 초대장 주문서는 기존 설명과 동작을 유지한다.

## 관리자 편집

상품명·가격·사진 주소·분류·안내, 옵션 묶음 이름과 선택지/추가금, 직접 입력 제공 여부, 새 묶음 추가/삭제, 전시/주문 중지를 수정한다. 가격/옵션을 바꾸면 상품 revision을 올리고 오래된 수정/주문 양식을 거부한다. 발급된 행사 링크에는 이전 옵션/가격을 유지한다. 51/52개 작품 선택지와 원본 여러 옵션 묶음을 보존한다.

## 독립 Render 검증 환경

`render.shop-verification.yaml`만 별도 Blueprint로 사용한다. 기존 render.yaml과 main 서비스는 변경하지 않는다.

- workspace: 사용자 확인한 Simba's workspace.
- source: codex/shop-backend; 서비스 이름 bellavita-shop-verification; autoDeployTrigger off; Blueprint autoSync false; create_new.
- paid 0.5 CPU / 512MB 단일 인스턴스, Oregon, 별도 1GB 디스크 /var/data.
- TAESAN_DATA=/var/data/shop-verification. 마운트/경로 확인이 안 되면 검증 서버가 시작을 거부한다.
- `python shop_verification.py`는 실결제·알림·영수증·원장 쓰기·공장 전달을 강제로 끄고, 결제 요청 어댑터를 로컬 모의 페이지로 교체한다. 운영 가맹점 키를 복사하지 않는다.
- TAESAN_PUBLIC_URL은 Render가 제공한 RENDER_EXTERNAL_URL에서 설정한다.
- `/verification`: 실결제/실발송 꺼짐과 디스크 상태의 비밀값 없는 진단.
- 검증 주문은 로컬 모의 결제 페이지의 결제/부분취소/전체취소 버튼으로 통보 규격을 통과시켜 처리한다. 32는 요청취소, 승인 전체취소는 64로 구분한다.
- Render Shell에서 `python shop_verification.py --admin-link`를 실행하면 10분 관리자 링크를 발급한다. URL의 서명은 비밀 접근 정보로 취급한다.
- 백업 경로 /var/data/shop-verification-backups는 데이터 디렉터리 밖이지만 같은 디스크다. 외부 재해복구 백업으로 간주하지 않는다. 운영 전 별도 외부 저장소 설정이 필요하다.
- 적용 전 Blueprint 계획 검토가 필요하며 실제 Render 재배포/디스크 보존 결과는 적용 후 검증한다. 계획 수립이나 로컬 재시작 검증을 Render 검증 성공으로 기록하지 않는다.

## 검증 재현

```
python -m unittest discover -s tests -q
.venv/bin/python tests/browser_shop_catalog.py
python tests/rehearse_catalog.py --output /새/검증/경로
python tests/rehearse_shop.py --output /새/기존업무/검증/경로
```

모바일/데스크톱 검증은 Python HTTP 백엔드를 대상으로 한다. 별도 Sites 샘플의 브라우저 검증으로 대체하지 않는다.

## 페이앱 공식 문서 대조

2026-11-14 https://api.payapp.kr/dev_center/dev_center01.html 확인. 서버 FORM POST /oapi/v2, 요청 hash sha256(userid+requestdate+requestkey), 통보 hash sha256(userid+responsedate+requestkey), userid/linkkey/linkval/원결제금액 대조, var1/mul_no 연결, checkretry=y 및 SUCCESS 응답, 4 승인·9/64 승인취소·70/71 부분취소의 orig_mul_no/orig_price를 대조했다. 8/32 요청취소와 승인취소를 구분한다. 기존 중복/역순 통보·타임아웃 대조 로직을 재사용한다.

이번에는 실제 가맹점 API 호출/결제/취소, 실발송, 현금영수증 실발급, 운영 DB·운영 시트·프린터 실행을 하지 않았다. 사용자 설정 완료 가정은 실제 거래 성공 증거를 대신하지 않는다.

## 이미지 규격

공개 판매 옵션은 600×400mm와 700×467mm(약 3:2). 족자 약 2:3은 공식 구성사진에서 추정한 화면 비율로, 실제 제작 mm 치수는 미확정이다. 썸네일은 생성 이미지의 비율에 맡기지 않고 별도 CSS 상자의 3:2 / 2:3을 적용한다. 제작용 PNG/PDF 치수를 이번 썸네일 비율로 변경하지 않는다.

## 검증 서버 원격 점검

SHOP_VERIFICATION_REVIEW_KEY는 검증 서버에만 개별 환경변수로 설정하며 저장소/Blueprint에는 값이 없다. Blueprint에 생략된 기존 서비스 변수는 이후 동기화에서 보존한다. /verification/review는 Bearer 인증 후 디스크 마운트·SQLite 무결성·건수·모의 결제 상태·자동 백업·프로세스 시작 식별자를 조회한다. 같은 경로의 backup_restore POST는 새 백업과 임시 복구 검증을 수행하며 실제 DB는 덮어쓰지 않는다. /verification/review/admin은 인증 후 10분 관리자 링크를 반환한다. 이 점검 경로는 shop_verification.py에서만 등록되며 운영 agent.py에는 없다.
