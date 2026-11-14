# 두 번째 다섯 후속 작업 검증 (2026-11-14)

기존 초안 PR #20, `codex/shop-backend` → `main`. 착수 HEAD `22475ba373c0175dd1212a5d1e7cbf0f8a144f44`.
최신 main `9348afabf049322b2bf3f061d511f5848d7aa1bf`를 다시 확인했다. 이번 작업 중 main 병합/운영 배포 없음.

## 완료한 승인 묶음

1. 담당 직원 배정·변경 이력·버전별 인수 확인, 이전 링크 차단.
2. 행사일·현재 단계·담당자 일정표와 오래된 주문 직접 조회.
3. 배송 사진 검증/정규화·직원 업로드·관리자 공개·고객 사진 확인.
4. 기간별 주문·취소·잔액 Excel 및 원장 ACK 참조 중복 점검.
5. 비밀값 비노출 설정 진단·별도 SQLite 모의 검증·리포트 다운로드.

사용법과 구체적인 권한·집계 범위는 [운영 안내](shop-delivery-suite.md)를 참조한다.

## 실행 결과

- `python -m unittest discover -s tests -q`: **114개 통과**. 신규 15개 포함.
- 배정: 오래된 버전·이전 공용/담당 링크·제한 역할·미등록 직원 차단, 인수 재요청 1행 유지, 취소 후 인수 차단.
- 일정: 기한 경과/24시간 이내 정렬, 담당 필터, 잘못된 기간 거절, 최근 200건 밖의 주문 열기.
- 사진: 실제 multipart HTTP, EXIF 제거, 동일 사진 중복 방지, 비공개/공개/숨김, 직원 공개 금지,
  주문·버전·Origin에 묶인 고객 확인, HTML/애니메이션/큰 파일 거절, 파일 변조 차단.
- 정산: 부분취소→전체취소 이중 가산 방지, 승인 전 요청취소 금전 합계 제외,
  문자열 수식 실행 방지, 고유 참조 집계·주문 간 중복 참조 표시, 기존 초대장 제외.
- 진단: 실제 네트워크 호출 금지 상태에서 로컬 모의 검증 통과, 비밀값 리포트 비노출, 실제 거래 미실행 구분.
- 백업/복구: 배정·사진·진단 기록과 이미지 바이트 보존, 변조된 사진은 복구 검증 실패 처리.
- `.venv/bin/python tests/browser_delivery_suite.py`: Chromium 375px에서 배정 → 인수 → 배송 완료 →
  사진 업로드 → 관리자 공개 → 고객 확인 → 정산 다운로드 → 진단. 가로 넘침/JavaScript 오류 없음.
  [브라우저 결과](validation/delivery-suite-mobile.json).

## 확인 링크

- [샘플 관리자·일정표](https://bellavita-shop-sample.shrewdmaxim1.chatgpt.site/?view=admin)
- [샘플 주문서](https://bellavita-shop-sample.shrewdmaxim1.chatgpt.site/?product=wedding-sunflower&offer=0)
- [모의 정산 Excel](https://bellavita-shop-sample.shrewdmaxim1.chatgpt.site/api/settlement.xlsx?from=2026-11-01&to=2026-12-31)
- [체험용 진단 JSON](https://bellavita-shop-sample.shrewdmaxim1.chatgpt.site/api/diagnostics)

기존 소유자 전용 Site와 기존 기록을 유지했다. Site는 Python 운영 DB와 분리된 모의 체험이다.
Site의 배송 사진은 `실제 설치 사진이 아닙니다`라고 표시한 합성 체험 이미지다.
실제 파일 업로드·이미지 검증·서명 권한은 PR의 Python 백엔드에서 검증했다.
Site의 진단은 체험 저장소 상태이며 Python 서버의 가맹점 설정 검사 결과를 대신하지 않는다.

Site 소스 `b3d10f19e4ad2245b361bb105c06f51dfb3bc7ed`, 배포
`appgdep_6af801ee3c188191becba9b05b4f96c8` 성공.
Node/JSDOM 테스트 6개, 생성 XLSX의 한국 날짜·잔액·문자열 수식 방지를 Python openpyxl로 교차 검증했다.
실제 호스팅 API에서 기존 기록, 배정/인수, 검수 후 배송, 공개 이미지 고객 확인, 숨김 후 접근 차단,
정산 XLSX 읽기, 진단 `live_payment=not_run`을 확인했다. 테스트 주문은 최종 모의 취소 상태다.

## 발견 사항과 재검증

- 다중 테스트 주문에 같은 모의 결제번호가 배정돼 UNIQUE 오류가 발생했다. 주문별 번호를 분리하고
  접수 성공을 먼저 검사하도록 수정한 뒤 재실행했다. 운영 DB의 중복 결제번호 거절은 유지했다.
- 서명 마지막 글자를 고정 문자로 바꾸는 테스트가 우연히 원문과 같을 수 있었다. 항상 다른 값으로 바꿔 재검증했다.
- 새 배정 양식 추가 후 JSDOM의 첫 양식 선택이 부분취소 양식과 달라졌다. 목적 필드로 선택해 재검증했다.
- 오래된 긴급 주문의 일정 링크가 최근 200건 목록 밖일 수 있어 정확한 주문번호 조회를 추가했다.
- 고객에게 직원용 알림톡 링크가 전달되지 않도록 채널별 링크를 분리하고 권한 테스트를 추가했다.
- 잘못된 비 ASCII 확인 서명을 형식 검사로 거절하고, DB의 제한 역할을 환경 목록보다 우선하도록 보완했다.

실결제·실발송·실제 배송·고객 실사진·운영 원장 쓰기·Render 운영 배포는 수행하지 않았다.
Sites 실제 브라우저 렌더링은 도구 제약으로 미검증이며 Node/JSDOM·호스팅 API와 Python 모바일 검증을 구분한다.
