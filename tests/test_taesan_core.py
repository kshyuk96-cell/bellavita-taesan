"""Include portable core regressions in normal test discovery."""
from pathlib import Path


def load_tests(loader, tests, pattern):
    root = Path(__file__).resolve().parents[1]
    return loader.discover(str(root / 'taesan_core' / 'tests'),
                           pattern='test_*.py', top_level_dir=str(root))
