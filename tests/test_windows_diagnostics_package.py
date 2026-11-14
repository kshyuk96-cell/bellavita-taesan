import tempfile,unittest,zipfile
from pathlib import Path
from windows_diagnostics.build import build,FILES

class DiagnosticPackageTests(unittest.TestCase):
    def fixture(self,root):
        source=Path(__file__).resolve().parents[1]/'windows_diagnostics'
        for name in FILES:(root/name).write_bytes((source/name).read_bytes())
    def test_package_excludes_local_reports_even_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root);(root/'diagnostic-detail-local.txt').write_text('synthetic private log')
            out=build(root/'diagnostics.zip',root)
            with zipfile.ZipFile(out) as archive:
                self.assertEqual(set(archive.namelist()),set(FILES));self.assertIsNone(archive.testzip())
                self.assertNotIn(b'synthetic private log',b''.join(archive.read(n) for n in archive.namelist()))
    def test_incorrect_powershell_text_encoding_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root);path=root/'diagnose.ps1';path.write_bytes(path.read_bytes()[3:])
            with self.assertRaises(ValueError):build(root/'diagnostics.zip',root)
    def test_literal_escaped_command_lines_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root);path=root/'START_DIAGNOSE.cmd';path.write_bytes(path.read_bytes().replace(b'\r\n',b'\\r\\n'))
            with self.assertRaises(ValueError):build(root/'diagnostics.zip',root)
