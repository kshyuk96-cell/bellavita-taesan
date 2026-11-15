# 기존 공통 대화 엔진과 업무 조회 연결

`manager_bridge/conversation_tools.py`는 PR57의 `taesan_core`를 다시 작성하지 않고 기존 `ProjectAdapters`에 연결한다. 등록 도구는 `erp.orders`, `shop.orders`, `hope.products`, `hope.inquiries`, `association.access`, `association.obituary` 여섯 읽기 도구뿐이다. 주문 수정·취소·결제·발송·이벤트 갱신 도구는 등록하지 않는다.

`principal_for_token(store, registry, token, project)`는 현재 대표/관리자 프로젝트 권한을 확인한 뒤 reader 역할만 만든다. subject는 현재 인증 주체 해시와 프로젝트의 합성 해시다. 같은 벨라비타의 ERP와 쇼핑몰도 대화 맥락과 요청 영수증이 섞이지 않는다. 고객 직원 로그인이나 원본 서비스 권한은 별도의 실제 공급자 인증을 통과해야 하며 이 변환으로 새 권한을 얻지 않는다.

`build_registry(adapters, token, project, config_fingerprint)`는 요청 단위의 서버 등록부를 만든다. 도구 실행 전후 현재 인증·회사·프로젝트·설정 지문을 확인한다. 토큰을 도구 인수나 응답에 전달하지 않는다. 이 등록부는 요청의 인증값에 묶이므로 전역 공유 캐시로 두지 않는다. 게이트웨이는 Core의 이전 응답 재사용 경로에서도 인증이 유지되도록 `Core.handle` 전후에 같은 현재 주체와 설정을 확인해야 한다.

도구 필드는 원본 조회 계약을 따른다. 날짜는 ISO 날짜, 표시 수는 1~100, 상태는 해당 서비스의 실제 상태만 허용한다. 쇼핑몰 날짜 검색은 원본 연결이 없어 제공하지 않는다. 부고 ID가 빠지면 기존 Core의 빠진 필드 질문을 사용하며 원본 접근 승인 없이 조회하지 않는다. 완료된 읽기의 등록 필터만 같은 도구의 명시적인 후속 요청에 이어진다.

결과에는 provider_state, 제한된 원본 items, 실제 적용 query, 소스 계약 커밋·읽기 참조·관측 시각을 보존한다. 슬래시가 있는 제공자 참조는 Core Evidence의 불투명 해시 receipt로 연결하고 실제 관측 시각은 유지한다. 미연결·권한 부족·불명확을 실제 0건으로 바꾸지 않는다. Core 크기 제한을 초과한 응답은 일부만 잘라 완전한 조회라고 보고하지 않고 좁혀 조회할 수 있는 미확인 결과로 남긴다.

회사 계약은 기존 Companies/Company를 읽기 도구 범위에서만 재사용한다. 실제 commerce 설정·운영 플래그·고객 소스는 바꾸지 않는다. 시험은 모의 제공자와 전용 임시 DB에서 수행했으며 실제 서비스 인증이나 운영 조회 성공의 증거가 아니다.
