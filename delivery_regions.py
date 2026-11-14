"""Conservative province identification for delivery alerts; never infer from sender names."""
import os,re

PROVINCES={
 '서울':('서울특별시','서울시','서울'), '경기':('경기도','경기'), '인천':('인천광역시','인천시','인천'),
 '부산':('부산광역시','부산시','부산'), '대구':('대구광역시','대구시','대구'),
 '대전':('대전광역시','대전시','대전'), '울산':('울산광역시','울산시','울산'),
 '광주':('광주광역시',), '세종':('세종특별자치시','세종시','세종'),
 '강원':('강원특별자치도','강원도','강원'), '충북':('충청북도','충북'), '충남':('충청남도','충남'),
 '전북':('전북특별자치도','전라북도','전북'), '전남':('전라남도','전남'),
 '경북':('경상북도','경북'), '경남':('경상남도','경남'), '제주':('제주특별자치도','제주도','제주시','서귀포시','제주')}
# Unambiguous municipality names; ambiguous 광주/고성/중구 alone deliberately excluded.
CITIES={
 '경기':'수원시 성남시 의정부시 안양시 부천시 광명시 평택시 동두천시 안산시 고양시 과천시 구리시 남양주시 오산시 시흥시 군포시 의왕시 하남시 용인시 파주시 이천시 안성시 김포시 화성시 양주시 포천시 여주시 연천군 가평군 양평군',
 '강원':'춘천시 원주시 강릉시 동해시 태백시 속초시 삼척시 홍천군 횡성군 영월군 평창군 정선군 철원군 화천군 양구군 인제군 양양군',
 '충북':'청주시 충주시 제천시 보은군 옥천군 영동군 증평군 진천군 괴산군 음성군 단양군',
 '충남':'천안시 공주시 보령시 아산시 서산시 논산시 계룡시 당진시 금산군 부여군 서천군 청양군 홍성군 예산군 태안군',
 '전북':'전주시 군산시 익산시 정읍시 남원시 김제시 완주군 진안군 무주군 장수군 임실군 순창군 고창군 부안군',
 '전남':'목포시 여수시 순천시 나주시 광양시 담양군 곡성군 구례군 고흥군 보성군 화순군 장흥군 강진군 해남군 영암군 무안군 함평군 영광군 장성군 완도군 진도군 신안군',
 '경북':'포항시 경주시 김천시 안동시 구미시 영주시 영천시 상주시 문경시 경산시 의성군 청송군 영양군 영덕군 청도군 고령군 성주군 칠곡군 예천군 봉화군 울진군 울릉군',
 '경남':'창원시 진주시 통영시 사천시 김해시 밀양시 거제시 양산시 의령군 함안군 창녕군 남해군 하동군 산청군 함양군 거창군 합천군'}

def province(location):
    text=str(location or '')
    # Explicit province tokens must occur at address boundaries, not inside venue/company names.
    candidates=set()
    for name,aliases in PROVINCES.items():
        for alias in aliases:
            end=r'(?=\s|$|[)\],/]|[가-힣]{1,6}(?:시|군|구)\s)' if len(alias)>2 else r'(?=\s|$)'
            if re.search(r'(?:^|[\s(\[/,])'+re.escape(alias)+end,text):candidates.add(name)
    # Bare 광주 is ambiguous with 경기도 광주시, unless a metropolitan district follows.
    if re.search(r'(?:^|[\s(\[/,])광주\s+(?:동구|서구|남구|북구|광산구)(?:\s|$)',text):candidates.add('광주')
    if len(candidates)==1:return next(iter(candidates))
    if len(candidates)>1:return ''
    for name,places in CITIES.items():
        if any(re.search(r'(?:^|[\s(\[/,])'+place+r'(?=\s|$|[)\],/])',text) for place in places.split()):candidates.add(name)
    return next(iter(candidates)) if len(candidates)==1 else ''

def delivery_alert(location,*,scope=None):
    scope=scope or os.getenv('TAESAN_REGIONAL_SCOPE','outside_capital')
    if scope not in {'outside_capital','outside_gyeonggi'}:raise ValueError('지방 배송 알림 범위를 확인해 주세요.')
    region=province(location)
    if not region:return {'region':'','status':'unknown','message':'배송지역 확인 필요: 주소를 확인해 주세요.'}
    exempt={'서울','경기','인천'} if scope=='outside_capital' else {'경기'}
    if region in exempt:return {'region':region,'status':'local','message':''}
    return {'region':region,'status':'nonlocal','message':f'⚠ 지방 배송 확인 필요 ({region}): 배송 가능 여부·설치 마감·추가 배송비를 확인해 주세요.'}
