"""Task-bound asset receipts; no bytes, customer text, or rendering assertions.

Worker receipts are current-lease claims. Manager observations are explicitly
separate attestations with evidence references, not proof a renderer used a font.
"""
import json
import re

from .asset_registry import _identity, _expected
from .store import Conflict, NotFound, canonical_json, digest, _reject_credentials

_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}')
_REFERENCE = re.compile(r'[A-Za-z0-9_:/#.-]{1,500}')


def _refs(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 50:
        raise ValueError('정확한 에셋 참조를 1~50개 지정해 주세요.')
    refs, seen = [], set()
    for item in values:
        if not isinstance(item, dict) or set(item) != {'asset_id', 'version', 'sha256'}:
            raise ValueError('에셋 ID·버전·SHA256만 참조로 남겨주세요.')
        _identity(item['asset_id'], item['version'])
        _expected(item['sha256'])
        key = (item['asset_id'], item['version'])
        if key in seen:
            raise ValueError('동일한 에셋 버전 참조를 중복 기록하지 마세요.')
        seen.add(key)
        refs.append(dict(item))
    return sorted(refs, key=lambda item: (item['asset_id'], item['version'], item['sha256']))


class AssetUsage:
    def __init__(self, store, registry):
        if registry.store.path != store.path:
            raise ValueError('동일한 관리 저장소의 에셋 레지스트리가 필요해요.')
        self.store, self.registry = store, registry
        with store._transaction() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS bridge_asset_usage (
                task TEXT NOT NULL,version INTEGER NOT NULL,project TEXT NOT NULL,
                provenance TEXT NOT NULL,usage_key TEXT NOT NULL,payload TEXT NOT NULL,
                payload_digest TEXT NOT NULL,actor TEXT NOT NULL,recorded REAL NOT NULL,
                PRIMARY KEY(task,version,provenance,usage_key))''')
            c.execute('''CREATE TABLE IF NOT EXISTS bridge_asset_usage_refs (
                task TEXT NOT NULL,version INTEGER NOT NULL,provenance TEXT NOT NULL,usage_key TEXT NOT NULL,
                project TEXT NOT NULL,asset_id TEXT NOT NULL,asset_version TEXT NOT NULL,sha256 TEXT NOT NULL,
                PRIMARY KEY(task,version,provenance,usage_key,asset_id,asset_version))''')
            c.execute('CREATE INDEX IF NOT EXISTS bridge_asset_usage_lookup '
                      'ON bridge_asset_usage_refs(project,asset_id,asset_version,sha256)')

    def record_worker_usage(self, token, task_id, version, manifest_digest, refs, lease_token, *, usage_key='default'):
        return self._record(token, task_id, version, manifest_digest, refs, 'worker_claim', usage_key,
                            lease_token=lease_token)

    def record_manager_observation(self, token, task_id, version, manifest_digest, refs, evidence_reference,
                                   *, usage_key='default'):
        if not isinstance(evidence_reference, str) or not _REFERENCE.fullmatch(evidence_reference):
            raise ValueError('원문 대신 독립 관찰 근거의 참조를 남겨주세요.')
        return self._record(token, task_id, version, manifest_digest, refs, 'manager_observation', usage_key,
                            evidence_reference=evidence_reference)

    def _record(self, token, task_id, version, manifest_digest, refs, provenance, usage_key,
                *, lease_token=None, evidence_reference=None):
        refs = _refs(refs)
        if not isinstance(usage_key, str) or not _KEY.fullmatch(usage_key):
            raise ValueError('사용 기록의 대조 키를 확인해 주세요.')
        task = self.store.get_task(token, task_id)
        if type(version) is not int or task['version'] != version or task['manifest_digest'] != manifest_digest:
            raise Conflict('에셋을 사용한 정확한 작업 버전과 명세를 대조해야 해요.')
        project = task['project']
        # Public registry access validates role/scope and the approved bytes.
        for ref in refs:
            self.registry.resolve(token, project, ref['asset_id'], ref['version'], ref['sha256'])
        payload = {'task_id': task_id, 'task_version': version, 'manifest_digest': manifest_digest,
                   'project': project, 'provenance': provenance, 'usage_key': usage_key,
                   'assets': refs, 'evidence_reference': evidence_reference,
                   'rendering_execution_verified': False}
        _reject_credentials(payload, token, lease_token)
        fingerprint = digest(payload)
        with self.store._transaction() as c:
            actor = self.store._authenticate(c, token)
            row = self.store._get(c, actor, task_id)
            if row['version'] != version or row['manifest_digest'] != manifest_digest:
                raise Conflict('사용 기록의 작업 범위가 바뀌었어요.')
            if provenance == 'worker_claim':
                self.store._role(actor, ('worker',))
                self.store._lease(row, actor, lease_token)
                if row['state'] != 'running' or row['lease_expires'] <= self.store._now():
                    raise Conflict('실행 중인 본인 작업의 유효 임대권이 필요해요.')
            else:
                self.store._role(actor, ('owner', 'manager'))
                if row['worker_subject'] is None or row['state'] not in (
                        'running', 'reported', 'reviewed', 'cancelled', 'needs_verification'):
                    raise Conflict('실행 이력이 있는 작업에만 관리자 관찰을 기록할 수 있어요.')
            # Revalidate status and cache within the write transaction, avoiding
            # a nested registry DB transaction and an approval/claim race.
            with self.registry._directory() as directory:
                for ref in refs:
                    asset = c.execute('SELECT * FROM bridge_assets WHERE project=? AND asset_id=? AND version=?',
                                      (project, ref['asset_id'], ref['version'])).fetchone()
                    if not asset:
                        raise NotFound('작업 프로젝트에 지정한 에셋이 없어요.')
                    if asset['status'] != 'approved' or asset['sha256'] != ref['sha256']:
                        raise Conflict('승인된 정확한 에셋 버전만 기록할 수 있어요.')
                    self.registry._read(directory, ref['sha256'], asset['size'])
            old = c.execute('SELECT * FROM bridge_asset_usage WHERE task=? AND version=? AND provenance=? AND usage_key=?',
                            (task_id, version, provenance, usage_key)).fetchone()
            if old:
                if old['payload_digest'] != fingerprint:
                    raise Conflict('동일한 사용 기록을 다른 에셋이나 근거로 덮어쓰지 않았어요.')
                return dict(json.loads(old['payload']), actor=old['actor'], recorded_at=old['recorded'], duplicate=True)
            now = self.store._now()
            c.execute('INSERT INTO bridge_asset_usage VALUES (?,?,?,?,?,?,?,?,?)',
                      (task_id, version, project, provenance, usage_key, canonical_json(payload), fingerprint,
                       actor['subject'], now))
            for ref in refs:
                c.execute('INSERT INTO bridge_asset_usage_refs VALUES (?,?,?,?,?,?,?,?)',
                          (task_id, version, provenance, usage_key, project, ref['asset_id'], ref['version'], ref['sha256']))
            self.store._event(c, task_id, actor['subject'], 'asset_usage_recorded', row['state'], payload)
            return dict(payload, actor=actor['subject'], recorded_at=now, duplicate=False)

    def list_by_asset(self, token, project, asset_id, asset_version, sha256, *, limit=100):
        _identity(asset_id, asset_version)
        _expected(sha256)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('조회 개수는 1~200개여야 해요.')
        with self.store._transaction() as c:
            actor = self.store._authenticate(c, token)
            self.store._role(actor, ('owner', 'manager'))
            self.store._project_access(c, actor, project)
            rows = c.execute('''SELECT u.* FROM bridge_asset_usage u JOIN bridge_asset_usage_refs r
                ON u.task=r.task AND u.version=r.version AND u.provenance=r.provenance AND u.usage_key=r.usage_key
                WHERE r.project=? AND r.asset_id=? AND r.asset_version=? AND r.sha256=?
                ORDER BY u.recorded DESC,u.task,u.provenance,u.usage_key LIMIT ?''',
                (project, asset_id, asset_version, sha256, limit + 1)).fetchall()
            return {'records': [dict(json.loads(row['payload']), actor=row['actor'], recorded_at=row['recorded'])
                                for row in rows[:limit]], 'truncated': len(rows) > limit}
