"""Conservative regression recommendations from a trusted local registry.

This module never runs commands. Task descriptions cannot supply argv or mapping
rules. Unknown paths select the full fallback; authorization and approval checks
are always present. A selected plan is not evidence that validation has passed.
"""
from pathlib import PurePosixPath


DEFAULT_REQUIRED = ('authorization_regression', 'approval_regression')


class SelectionError(ValueError):
    pass


def _path(value, *, prefix=False):
    if (not isinstance(value, str) or not value or len(value) > 500
            or value.startswith('/') or '\\' in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(char in value for char in '*?[]')):
        raise SelectionError('정확한 저장소 상대 경로가 필요해요.')
    cleaned = value[:-1] if prefix and value.endswith('/') else value
    if any(part in ('', '.', '..', '.git') for part in cleaned.split('/')):
        raise SelectionError('상위 경로·중복 구분자·Git 메타데이터는 선택할 수 없어요.')
    if str(PurePosixPath(cleaned)) != cleaned:
        raise SelectionError('정규 상대 경로가 필요해요.')
    return value


def _names(value):
    if (not isinstance(value, (list, tuple)) or not value or len(value) > 100
            or any(not isinstance(name, str) or not name or len(name) > 100 for name in value)
            or len(set(value)) != len(value)):
        raise SelectionError('중복 없는 고정 검증 이름이 필요해요.')
    return tuple(value)


class RegressionSelector:
    def __init__(self, checks, mapping, *, required_checks=DEFAULT_REQUIRED, full_checks=None):
        if not isinstance(checks, dict) or not checks or len(checks) > 200:
            raise SelectionError('관리자가 지정한 검증 명령 등록부가 필요해요.')
        self.checks = {}
        for name, argv in checks.items():
            _names([name])
            if (not isinstance(argv, (list, tuple)) or not argv or len(argv) > 100
                    or any(not isinstance(arg, str) or not arg or '\x00' in arg or '\n' in arg or '\r' in arg for arg in argv)
                    or not PurePosixPath(argv[0]).is_absolute()):
                raise SelectionError('고정된 절대 경로 실행 명령 배열이 필요해요.')
            self.checks[name] = tuple(argv)
        self.required = _names(required_checks)
        self.full = _names(full_checks if full_checks is not None else tuple(checks))
        if not set(DEFAULT_REQUIRED) <= set(self.required) or not set(self.required) <= set(self.full):
            raise SelectionError('보안·승인 필수 검사를 전체 검증에도 포함해 주세요.')
        if not set(self.full) <= set(self.checks):
            raise SelectionError('전체 검증에 등록되지 않은 명령이 있어요.')
        if not isinstance(mapping, dict) or len(mapping) > 1000:
            raise SelectionError('관리자가 지정한 경로 연결표가 필요해요.')
        self.mapping = []
        for pattern, names in mapping.items():
            pattern = _path(pattern, prefix=True)
            names = _names(names)
            if not set(names) <= set(self.checks):
                raise SelectionError('경로에 등록되지 않은 검증 이름이 있어요.')
            self.mapping.append((pattern, names))

    def plan(self, changed_paths):
        if not isinstance(changed_paths, (list, tuple)) or len(changed_paths) > 1000:
            raise SelectionError('변경 파일 목록은 1000개 이내여야 해요.')
        paths = sorted({_path(path) for path in changed_paths})
        selected = set(self.required)
        unknown = []
        for path in paths:
            matches = [names for pattern, names in self.mapping
                       if path == pattern or (pattern.endswith('/') and path.startswith(pattern))]
            if not matches:
                unknown.append(path)
            for names in matches:
                selected.update(names)
        if unknown:
            selected.update(self.full)
        names = list(self.required) + sorted(selected - set(self.required))
        return {'checks': [{'name': name, 'argv': list(self.checks[name])} for name in names],
                'changed_paths': paths, 'unknown_paths': unknown,
                'full_fallback_selected': bool(unknown), 'selection_only': True,
                'commands_executed': False, 'full_validation_completed': False}
