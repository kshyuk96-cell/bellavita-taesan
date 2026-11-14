"""Include the independent manager protocol in the repository's normal CI suite."""
from pathlib import Path


def load_tests(loader, tests, pattern):
    root = Path(__file__).resolve().parents[1]
    return loader.discover(str(root / 'manager_bridge' / 'tests'), pattern='test_*.py',
                           top_level_dir=str(root))
