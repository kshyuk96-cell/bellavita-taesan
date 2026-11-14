"""Durability across actual server process replacement, with fixture credentials."""
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from manager_bridge.control import request, write_secret
from manager_bridge.native_handoff import NativeHandoff
from manager_bridge.store import Store
from manager_bridge.worker import WorkerClient


class ProcessRestartTests(unittest.TestCase):
    def setUp(self):
        self.process = None
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.stop_server)
        self.root = Path(self.directory.name)
        self.data = self.root/'queue.sqlite3'
        self.store = Store(self.data)
        self.owner = self.store.add_token('fixture-owner', 'owner', ['*'])
        self.store.register_project(self.owner, 'fixture', 'fixture/repository')
        self.manager = self.store.add_token('fixture-manager', 'manager', ['fixture'])
        self.worker = self.store.add_token('fixture-worker', 'worker', ['fixture'])
        self.worker_file = self.root/'worker.token'
        write_secret(self.worker_file, self.worker)
        self.start_server()

    def start_server(self):
        program = '''
import json,sys
from manager_bridge.server import make_server
from manager_bridge.store import Store
server=make_server(Store(sys.argv[1]),port=0)
print(json.dumps({'port':server.server_address[1]}),flush=True)
server.serve_forever()
'''
        # No Google/OpenAI/Render credentials or host proxy configuration enters.
        environment = {'PATH': os.defpath, 'HOME': str(self.root), 'PYTHONDONTWRITEBYTECODE': '1'}
        repository = Path(__file__).resolve().parents[2]
        self.process = subprocess.Popen([sys.executable, '-c', program, str(self.data)],
            cwd=repository, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ready, _, _ = select.select([self.process.stdout], [], [], 10)
        if not ready:
            self.fail('Isolated fixture server did not start')
        message = json.loads(self.process.stdout.readline())
        self.base = 'http://127.0.0.1:' + str(message['port'])

    def stop_server(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process.stdout.close()
            self.process.stderr.close()
            self.process = None

    def controller(self):
        return NativeHandoff(self.root/'native.sqlite3',
            WorkerClient(self.base, self.worker_file, 'fixture'), '/root/fixture_agent')

    def post(self, token, path, body):
        result = request(self.base, token, 'POST', path, body)
        self.assertTrue(result.get('ok'), result)
        return result['task']

    def test_restart_preserves_lease_report_and_review_without_redispatch(self):
        manifest = dict(project='fixture', repository='fixture/repository', base_sha='a'*40,
                        description='fixture callback only', checks=['fixture check'], kind='test')
        task = self.post(self.manager, '/v1/tasks', {'request_key': 'restart-1', 'manifest': manifest})
        approval = {'version': task['version'], 'digest': task['manifest_digest']}
        self.post(self.owner, '/v1/tasks/'+task['id']+'/approve', approval)
        native = self.controller()
        self.assertEqual(native.begin()['task_id'], task['id'])
        native.prepare_dispatch()
        native.dispatched('fixture-platform:single-dispatch')

        self.stop_server()
        self.start_server()
        recovered = self.controller()
        self.assertEqual(recovered.begin()['phase'], 'dispatched')
        self.assertEqual(recovered.snapshot()['task_id'], task['id'])
        report = {'head_sha': manifest['base_sha'], 'summary': 'fixture result only',
                  'checks': [{'name': 'fixture-check', 'passed': True}]}
        self.assertEqual(recovered.finish(report)['phase'], 'reported')

        self.stop_server()
        self.start_server()
        final = self.controller()
        self.assertEqual(final.reconcile()['phase'], 'reported')
        self.post(self.manager, '/v1/tasks/'+task['id']+'/review',
                  {**approval, 'evidence_reference': 'fixture-independent-review'})
        self.assertEqual(final.release_reviewed()['phase'], 'idle')
        with sqlite3.connect(self.data) as c:
            reports = c.execute("SELECT COUNT(*) FROM bridge_events WHERE action='worker_reported'").fetchone()[0]
            tasks = c.execute('SELECT COUNT(*) FROM bridge_tasks').fetchone()[0]
        self.assertEqual(reports, 1)
        self.assertEqual(tasks, 1)
