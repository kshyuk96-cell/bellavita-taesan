"""Client-only feature navigation; never submits an admin action."""
import json

FEATURES=[
 ('전체 주문 검색','주문번호 고객 연락처 문구 검색','.admin-search','#admin-orders'),
 ('행사 빠른 보기','오늘 이번주 일정','.event-shortcuts','#admin-orders'),
 ('업무 담당자·기한','미처리 우선순위 완료 재열기','.order-tasks','.task-board'),
 ('저장 검색','자주 쓰는 검색 필터','section[data-admin-feature-title="저장 검색"]','#admin-orders'),
 ('고객 응대 템플릿','응대 문구 등록 수정','.response-template-settings','#admin-settings'),
 ('응대 문구 즐겨찾기','자주 쓰는 문구 추가 해제','.response-favorites','#admin-settings'),
 ('상품 운영 메모','내부 메모 상품메모 주의사항','.product-note','#admin-products'),
 ('상품 수정','가격 옵션 전시 등록','[data-admin-product]','#admin-products'),
 ('행사별 판매 링크','가격 링크 발급 판매중지','#admin-products','#admin-products'),
 ('교대 요약 다운로드','인수인계 미처리 파일 내보내기','.shift-summary','#admin-orders'),
 ('주문 인수인계 점검표','인계 인수 체크','.operator-handover','#admin-orders'),
 ('결제 확인','페이앱 대조 취소 부분취소','.order-reference','#admin-orders'),
 ('배송 일정·담당 배정','배송 사진 설치 배정','#admin-delivery','#admin-delivery'),
 ('제작 검수','족자 문구 파일 승인','.order-timeline','#admin-orders'),
 ('정산·집계','매출 금액 정산','#admin-reports','#admin-reports'),
 ('백업 상태','복구 실패 누락','.backup-health','#admin-settings'),
 ('직원 교육','연습 모의 주문','section[data-admin-feature-title="직원 교육"]','#admin-settings'),
 ('직원 알림 설정','카톡 알림 전화번호','section[data-admin-feature-title="직원 주문 알림"]','#admin-settings'),
 ('교대 요약 보관 이력','보관 저장 과거 파일','.shift-history','#admin-orders'),
 ('운영 전 점검','실결제 알림톡 현금영수증 디스크 준비','.launch-check','#admin-settings')]


FEATURE_IDS=['order-search','event-dates','task-management','saved-search','response-template','response-favorite','product-note','product-edit','offer-links','shift-download','order-handover','payment-check','delivery','production','sales','backup','training','staff-notify','shift-history','launch-check']

def markup(favorites=''):return '<section class="admin-feature-search"><h2>관리자 기능 찾기</h2>'+favorites+'<label>기능 이름 검색<input id="admin-feature-query" type="search" maxlength="80" placeholder="예: 즐겨찾기, 교대, 상품 메모"></label><p>기능 위치로 이동합니다. 주문 내용 검색은 전체 주문 검색을 이용하세요.</p><p id="admin-feature-count" role="status"></p><div id="admin-feature-results"></div></section>'


SCRIPT=r'''
(()=>{const features=FEATURE_DATA;const input=document.getElementById('admin-feature-query');const results=document.getElementById('admin-feature-results');const status=document.getElementById('admin-feature-count');if(!input)return;
const norm=value=>value.toLowerCase().replace(/\s+/g,'');
function navigate(item){const direct=document.querySelector(item[2]);const target=direct||document.querySelector(item[3]);if(!target)return;let ancestor=target;while(ancestor){if(ancestor.tagName==='DETAILS')ancestor.open=true;ancestor=ancestor.parentElement;}target.tabIndex=-1;target.scrollIntoView({block:'start'});target.focus({preventScroll:true});status.textContent=direct?item[0]+' 위치입니다.':item[0]+' · 해당 주문을 검색한 뒤 상세를 펼쳐 주세요.';}
for(const button of document.querySelectorAll('[data-admin-feature]'))button.addEventListener('click',()=>{const item=features.find(item=>item[4]===button.dataset.adminFeature);if(item)navigate(item);});
input.addEventListener('input',()=>{results.replaceChildren();const terms=input.value.trim().toLowerCase().split(/\s+/).filter(Boolean);if(!terms.length){status.textContent='';return;}const found=features.filter(item=>terms.every(term=>norm(item[0]+' '+item[1]).includes(norm(term))));status.textContent=found.length?found.length+'개 기능 · 최대 10개 표시':'일치하는 기능이 없습니다.';for(const item of found.slice(0,10)){const button=document.createElement('button');button.type='button';button.textContent=item[0]+' 위치로 이동';button.addEventListener('click',()=>navigate(item));results.append(button);}});
})();
'''.replace('FEATURE_DATA',json.dumps([list(item)+[key] for key,item in zip(FEATURE_IDS,FEATURES)],ensure_ascii=False))
