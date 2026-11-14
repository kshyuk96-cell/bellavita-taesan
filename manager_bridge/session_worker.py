"""Pull approved tasks and queue them to one explicitly registered Codex session."""
import argparse
import json
import os
import stat
import time

from .session_dispatch import SessionTarget, dispatch_session
from .worker import WorkerClient, WorkerError


def load_registry(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
            raise ValueError('private registry required')
        value = json.loads(os.read(descriptor, 65537))
        if not isinstance(value, dict) or not value or len(value) > 100:
            raise ValueError('invalid registry')
        return {worker: SessionTarget(**target) for worker, target in value.items()}
    finally:
        os.close(descriptor)


class SessionRunner:
    def __init__(self, worker_id, registry, *, command=None):
        self.worker_id, self.registry, self.command = worker_id, registry, command

    def run(self, task, *, pulse):
        def current(candidate, target):
            return (candidate.get('id') == task.get('id')
                    and candidate.get('manifest_digest') == task.get('manifest_digest')
                    and candidate.get('version') == task.get('version')
                    and candidate.get('lease_token') == task.get('lease_token')
                    and target.project == task['manifest']['project'] and pulse())
        result = dispatch_session(self.worker_id, task, registry=self.registry,
                                  validate_task=current, runner=self.command)
        if result['status'] != 'queued':
            raise WorkerError('세션 전달이 미확인이라 보고를 완료 처리하지 않았어요.')
        return {'head_sha': task['manifest']['base_sha'],
                'summary': result['summary'] + ' 기준 커밋은 요청 기준이며 작업 결과 커밋이 아닙니다. 독립 검토 대기 상태입니다.',
                'checks': [{'name': 'session_message_queued', 'passed': result['status'] == 'queued'},
                           {'name': 'session_execution_verified', 'passed': False}]}


def main(argv=None):
    parser = argparse.ArgumentParser(description='등록한 Codex CLI 세션 연결 워커')
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--worker-id', required=True)
    parser.add_argument('--registry', required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    try:
        registry = load_registry(args.registry)
        if args.worker_id not in registry or registry[args.worker_id].project != args.project:
            raise ValueError('registered worker required')
        client = WorkerClient(args.base_url, args.token_file, args.project)
        runner = SessionRunner(args.worker_id, registry)
        while True:
            result = client.run_once(runner)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.once:
                return 0 if result['status'] in ('idle', 'reported') else 2
            if result['status'] not in ('idle', 'reported'):
                return 2  # An uncertain send is not repeated on the next poll.
            time.sleep(5)
    except KeyboardInterrupt:
        return 0
    except Exception:
        print(json.dumps({'status': 'unverified', 'retry_performed': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
