"""Keep authenticated read results from escaping after permission revocation.

The caller supplies a server-owned authorization callback, such as a fresh lookup
in the staff registry. Never derive this callback or its return value from model
output, request data, a document, or a transcript. This helper does not authenticate
an account itself and does not grant permissions. It only wraps side-effect-free
reads: a post-read denial cannot undo a write or a provider-side action.
"""


def guarded_read(read, *, authorized):
    """Check current access before and after the actual read; return only if both allow."""
    if not callable(read) or not callable(authorized):
        raise ValueError('인증된 읽기 연결을 확인해 주세요.')

    def require_access():
        try:
            allowed = bool(authorized())
        except Exception:
            allowed = False
        if not allowed:
            raise PermissionError('조회 권한을 확인하지 못해 결과를 표시하지 않았어요.') from None

    require_access()
    result = read()
    require_access()
    return result
