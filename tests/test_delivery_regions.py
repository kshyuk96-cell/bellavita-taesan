import unittest
from delivery_regions import delivery_alert,province

# Expectations authored independently of implementation tables.
POSITIVES=[('서울특별시 강남구 가상로 10','서울',False),('경기도 광주시 역동 10','경기',False),('경기 수원시 가상로 10','경기',False),('인천광역시 서구 가상로 10','인천',False),('부산광역시 해운대구 가상로 10','부산',True),('대구광역시 중구 가상로 10','대구',True),('광주광역시 북구 가상로 10','광주',True),('광주 광산구 가상로 10','광주',True),('대전광역시 서구 가상로 10','대전',True),('울산광역시 남구 가상로 10','울산',True),('세종특별자치시 가상로 10','세종',True),('강원특별자치도 춘천시 가상로 10','강원',True),('충청북도 청주시 가상로 10','충북',True),('충청남도 천안시 가상로 10','충남',True),('전북특별자치도 전주시 가상로 10','전북',True),('전라남도 순천시 가상로 10','전남',True),('경상북도 청도군 가상로 10','경북',True),('경상남도 김해시 가상로 10','경남',True),('제주특별자치도 제주시 가상로 10','제주',True),('수원시 가상로 10','경기',False),('김해시 가상로 10','경남',True)]
class DeliveryRegionTests(unittest.TestCase):
    def test_region_layout_variations(self):
        prefixes=['','가상예식장\n','가상예식장 / ','가상예식장 (','[테스트] ']
        suffixes=['',')','\n본관 3층']
        for address,region,nonlocal_ in POSITIVES:
            for pre in prefixes:
                for suffix in suffixes:
                    with self.subTest(address=address,pre=pre,suffix=suffix):
                        result=delivery_alert(pre+address+suffix)
                        self.assertEqual(result['region'],region)
                        self.assertEqual(result['status'],'nonlocal' if nonlocal_ else 'local')
    def test_ambiguous_or_conflicting_not_guessed(self):
        for value in ['광주시 가상로 10','광주호텔','고성군 가상로 10','중구 가상로','가상호텔','서울대학교','경기도 수원시 / 부산광역시 해운대구','']:
            with self.subTest(value=value):self.assertEqual(delivery_alert(value)['status'],'unknown')
    def test_gyeonggi_only_policy(self):
        self.assertEqual(delivery_alert('서울특별시 강남구',scope='outside_gyeonggi')['status'],'nonlocal')
        self.assertEqual(delivery_alert('인천광역시 서구',scope='outside_gyeonggi')['status'],'nonlocal')
        self.assertEqual(delivery_alert('경기도 광주시',scope='outside_gyeonggi')['status'],'local')
    def test_no_automatic_fee_or_driver_decision(self):
        result=delivery_alert('부산광역시 해운대구')
        self.assertEqual(set(result),{'region','status','message'})
        self.assertIn('추가 배송비',result['message'])

if __name__=='__main__':unittest.main()
