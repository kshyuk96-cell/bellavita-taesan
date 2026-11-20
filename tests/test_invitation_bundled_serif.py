"""The approved notice must retain its serif font without host font packages."""
from pathlib import Path
import unittest
from unittest.mock import patch
import erp_invitation_render as render

class BundledSerifTests(unittest.TestCase):
    def test_regular_uses_bundled_kr_serif_without_optional_plotting_package(self):
        with patch('importlib.util.find_spec',side_effect=AssertionError('optional plotting dependency must not be queried')):
            face=render.font(78)
        self.assertEqual(Path(face.path).resolve(),(render.BUNDLED_SERIF_DIR/'NotoSerifKR-Regular.otf').resolve())
        self.assertEqual(face.getname(),('Noto Serif CJK KR','Regular'))
        self.assertEqual(face.size,78)
        self.assertGreater(face.getlength('김영숙의 장녀 이다온 예식'),0)

    def test_no_host_fonts_keeps_serif_for_bold_requests(self):
        # Missing Linux Noto packages must never silently change to Nanum Gothic.
        original=Path.exists
        def exists(path):
            return False if str(path).startswith('/usr/share/fonts/') else original(path)
        with patch.object(Path,'exists',exists),patch('importlib.util.find_spec',side_effect=AssertionError('must use bundled serif')):
            face=render.font(32,bold=True)
        self.assertEqual(face.getname(),('Noto Serif CJK KR','Regular'))
        self.assertEqual(face.size,32)

    def test_subset_and_license_are_bundled(self):
        font=render.BUNDLED_SERIF_DIR/'NotoSerifKR-Regular.otf'
        self.assertLess(font.stat().st_size,3*1024*1024)
        license=(render.BUNDLED_SERIF_DIR/'NotoSerifKR-OFL.txt').read_text()
        self.assertIn('SIL OPEN FONT LICENSE Version 1.1',license)
        self.assertIn('Google',license)

if __name__=='__main__':unittest.main()
