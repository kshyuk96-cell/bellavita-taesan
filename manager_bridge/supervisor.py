"""Local manager verification and recovery guidance. No model or deployment calls.

Check argv comes from the trusted manager's local registry, never task prose or
worker output. Run on an isolated verification host: tests execute repository
code. A review binds the exact worker report plus independently read artifacts.
"""
import json
from pathlib import Path

from .store import Conflict, digest


class VerificationBlocked(RuntimeError):
    pass


def recovery_action(task):
    """Persisted server state is authoritative; never replay uncertain work."""
    state = task['state']
    return {
        'pending_approval': 'owner_development_approval',
        'queued': 'check_environment_then_claim',
        'running': 'reconcile_existing_worker',
        'reported': 'independent_review',
        'reviewed': 'development_verified_only',
        'needs_verification': 'inspect_existing_worktree_no_replay',
        'cancelled': 'confirm_worker_stopped_no_replay',
    }[state]


def verify_report(store, token, task_id, runner, check_commands):
    """Independent exact-artifact review, with local explicit check commands.

    This does not grant production approval. Existing generic review API remains
    an explicit human/manager evidence record; this path supplies stronger proof.
    """
    task = store.get_task(token, task_id)
    identity = store.authenticate(token)
    if identity['role'] not in ('owner', 'manager'):
        raise VerificationBlocked('관리자 검증 권한이 필요해요.')
    if task['state'] != 'reported':
        raise VerificationBlocked('보고된 작업만 독립 검증할 수 있어요.')
    manifest, report = task['manifest'], task['report']
    if (not isinstance(check_commands, dict) or not manifest['checks']
            or set(check_commands) != set(manifest['checks'])):
        raise VerificationBlocked('승인된 모든 검증 항목에 실제 검증 명령을 연결해 주세요.')
    for argv in check_commands.values():
        if (not isinstance(argv, (list, tuple)) or not argv or len(argv) > 100
                or any(not isinstance(arg, str) or not arg or '\x00' in arg for arg in argv)
                or not Path(argv[0]).is_absolute()):
            raise VerificationBlocked('관리자가 지정한 절대 경로 실행 명령이 필요해요.')
    checks = {item['name']: item['passed'] for item in report['checks']}
    required = ('codex_exit_zero', 'model_turn_completed', 'command_tools_succeeded',
                'approved_base_preserved', 'git_diff_check')
    if any(checks.get(name) is not True for name in required):
        raise VerificationBlocked('실행 실패 또는 도구 결과 미확인 작업은 완료 처리하지 않아요.')
    expected_root = runner.work_root.resolve(strict=True)
    target = expected_root / ('task-' + task['id'] + '-v' + str(task['version']))
    if target.is_symlink() or target.resolve(strict=True) != target:
        raise VerificationBlocked('고정된 작업폴더 위치를 확인해 주세요.')
    if runner._git(target, 'rev-parse', '--show-toplevel') != str(target):
        raise VerificationBlocked('검증 작업폴더가 Git 루트가 아니에요.')
    if runner._git(target, 'rev-parse', 'HEAD') != report['head_sha'] or report['head_sha'] != manifest['base_sha']:
        raise VerificationBlocked('보고된 커밋과 승인 기준이 달라요.')
    artifact_path = expected_root / ('artifacts-' + task['id']) / 'worktree-artifact.json'
    if artifact_path.parent.is_symlink() or artifact_path.is_symlink() or artifact_path.stat().st_size > 1_000_000:
        raise VerificationBlocked('검증 자료 파일 범위를 확인해 주세요.')
    recorded = json.loads(artifact_path.read_text())
    try:
        claimed = json.loads(report['summary'].split('\n', 1)[1])
    except (ValueError, IndexError):
        raise VerificationBlocked('작업 보고와 검증 자료 연결을 확인하지 못했어요.') from None
    before = runner.inspect_artifact(target, expected_root)
    if (before['git_diff_digest'] != recorded.get('git_diff_digest')
            or before['git_diff_digest'] != claimed.get('git_diff_digest')
            or recorded.get('worktree_path') != str(target)):
        raise VerificationBlocked('보고 이후 변경된 작업폴더는 완료 처리하지 않아요.')
    outcomes = []
    for name, argv in check_commands.items():
        try:
            result = runner.command(list(argv), cwd=target, env=runner._env(expected_root),
                                    capture_output=True, timeout=60, check=False)
        except Exception:
            raise VerificationBlocked('독립 검증 실행이 중단됐어요.') from None
        if result.returncode != 0:
            raise VerificationBlocked('독립 검증이 실패했어요.')
        # Never retain raw test output (may contain source data or secrets).
        outcomes.append({'name': name, 'argv_digest': digest(list(argv)), 'passed': True})
    after = runner.inspect_artifact(target, expected_root)
    if before['git_diff_digest'] != after['git_diff_digest']:
        raise VerificationBlocked('검증 중 결과물이 바뀌어 다시 대조해야 해요.')
    current = store.get_task(token, task_id)
    if current['state'] != 'reported' or digest(current['report']) != digest(report):
        raise Conflict('검증 중 작업 상태가 바뀌었어요.')
    evidence = {'task_id': task_id, 'version': task['version'], 'manifest_digest': task['manifest_digest'],
                'report_digest': digest(report), 'artifact_digest': before['git_diff_digest'], 'checks': outcomes}
    fingerprint = digest(evidence)
    folder = expected_root / ('artifacts-' + task_id)
    path = folder / ('independent-review-' + fingerprint + '.json')
    # Exclusive write preserves prior evidence and prevents a symlink overwrite.
    import os
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(evidence, output, ensure_ascii=False, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    return store.review(token, task_id, task['version'], task['manifest_digest'], 'independent-artifact:' + fingerprint)


def main(argv=None):
    """Local trusted manager entry point. It does not start a worker."""
    import argparse
    from .control import secret
    from .store import Store
    from .worker import CodexRunner
    parser = argparse.ArgumentParser(description='태산이 독립 검증·복구 현황')
    parser.add_argument('action', choices=('overview', 'recovery', 'verify'))
    parser.add_argument('--data', required=True, type=Path)
    parser.add_argument('--token-file', required=True, type=Path)
    parser.add_argument('--project', required=True)
    parser.add_argument('--task')
    parser.add_argument('--work-root', type=Path)
    parser.add_argument('--codex-home', type=Path)
    parser.add_argument('--checks-file', type=Path,
                        help='관리자 전용 JSON: 승인 검증 항목 이름 → 절대 경로 argv 배열')
    args = parser.parse_args(argv)
    try:
        token = secret(args.token_file)
        store = Store(args.data / 'manager.sqlite3')
        if args.action == 'overview':
            result = store.overview(token, args.project)
        else:
            task = store.get_task(token, args.task)
            actor = store.authenticate(token)
            if actor['role'] not in ('owner', 'manager') or task['manifest']['project'] != args.project:
                raise VerificationBlocked('대상 프로젝트 관리 권한이 필요해요.')
            if args.action == 'recovery':
                result = {'task_id': task['id'], 'state': task['state'],
                          'next_action': recovery_action(task), 'automatically_replayed': False}
            else:
                if not all((args.work_root, args.codex_home, args.checks_file)):
                    raise VerificationBlocked('검증 폴더와 관리자 검증 명령이 필요해요.')
                if args.checks_file.is_symlink() or args.checks_file.stat().st_mode & 0o077:
                    raise VerificationBlocked('관리자 전용 검증 설정 파일이 필요해요.')
                runner = CodexRunner({}, codex_home=args.codex_home, work_root=args.work_root)
                reviewed = verify_report(store, token, task['id'], runner, json.loads(args.checks_file.read_text()))
                result = {'task_id': reviewed['id'], 'state': reviewed['state'],
                          'operational_deployment_verified': False}
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
        return 0
    except Exception:
        print(json.dumps({'ok': False, 'error': 'manager_verification_required'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
