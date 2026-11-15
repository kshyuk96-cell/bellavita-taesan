"""Manager planning, bounded change proposals, and acknowledged digest previews.

No external messaging or automatic approval. Change requests preserve original
artifacts and always create a separately approved successor manifest.
"""
import json
import math
import secrets

from .store import Conflict, PermissionDenied, canonical_json, digest, _manifest, _KEY, _text, _reject_credentials


class Workflow:
    def __init__(self,store):
        self.store=store

    @staticmethod
    def create_tables(c):
        c.executescript('''
            CREATE TABLE IF NOT EXISTS bridge_task_planning (
                task TEXT PRIMARY KEY,priority INTEGER NOT NULL,due REAL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS bridge_change_requests (
                source TEXT NOT NULL,successor TEXT PRIMARY KEY,reason TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS bridge_digest_receipts (
                project TEXT NOT NULL,recipient TEXT NOT NULL,fingerprint TEXT NOT NULL,
                snapshot TEXT NOT NULL,PRIMARY KEY(project,recipient));
        ''')

    def plan(self,token,task_id,*,priority,due_at):
        if (type(priority) is not int or priority not in (0,1,2) or
                (due_at is not None and (type(due_at) not in (int,float) or not math.isfinite(due_at) or due_at<=0))):
            raise ValueError('우선순위 0~2와 실제 마감 시각을 지정해 주세요.')
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            row=self.store._get(c,actor,task_id)
            if row['state'] in ('reviewed','cancelled'):
                raise Conflict('종료된 작업의 기한을 변경하지 않아요.')
            c.execute('INSERT OR REPLACE INTO bridge_task_planning VALUES (?,?,?,?)',
                      (task_id,priority,due_at,self.store._now()))
            self.store._event(c,task_id,actor['subject'],'planning_changed',row['state'])
            return {'task_id':task_id,'priority':priority,'due_at':due_at,'execution_preempted':False}

    def propose_change(self,token,task_id,*,request_key,manifest,reason):
        manifest=_manifest(manifest);_text(reason,500,'변경 사유')
        _reject_credentials({'manifest':manifest,'reason':reason},token)
        if not isinstance(request_key,str) or not _KEY.fullmatch(request_key):
            raise ValueError('변경 요청 대조 키가 필요해요.')
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            source=self.store._get(c,actor,task_id)
            if manifest['project']!=source['project'] or manifest['repository']!=json.loads(source['manifest'])['repository']:
                raise Conflict('다른 프로젝트나 저장소로 지시를 바꾸지 않았어요.')
            existing=c.execute('SELECT * FROM bridge_tasks WHERE project=? AND request_key=?',
                               (source['project'],request_key)).fetchone()
            if existing:
                link=c.execute('SELECT * FROM bridge_change_requests WHERE successor=?',(existing['id'],)).fetchone()
                if (not link or link['source']!=task_id or link['reason']!=reason or
                        existing['manifest_digest']!=digest(manifest)):
                    raise Conflict('같은 변경 키의 내용이 달라요.')
                return self.store._public(existing)
            if source['state']=='cancelled':
                raise Conflict('이미 취소된 지시 대신 최신 후속 작업을 지정해 주세요.')
            # A verified candidate stays frozen; an active request is cancelled,
            # retaining worker/repository reservations until independent stop proof.
            if source['state']!='reviewed':
                c.execute("UPDATE bridge_tasks SET state='cancelled',cancel_reason='지시 변경 후속 검토',updated=? WHERE id=?",
                          (self.store._now(),task_id))
                c.execute('DELETE FROM bridge_task_route WHERE task=?',(task_id,))
                self.store._event(c,task_id,actor['subject'],'superseded','cancelled')
            new_id,now=secrets.token_hex(16),self.store._now()
            c.execute('''INSERT INTO bridge_tasks
                (id,project,request_key,manifest,manifest_digest,version,state,created,updated)
                VALUES (?,?,?,?,?,1,'pending_approval',?,?)''',
                (new_id,source['project'],request_key,canonical_json(manifest),digest(manifest),now,now))
            c.execute('INSERT INTO bridge_change_requests VALUES (?,?,?,?)',(task_id,new_id,reason,now))
            self.store._event(c,new_id,actor['subject'],'change_proposed','pending_approval',manifest)
            return self.store._public(self.store._get(c,actor,new_id))

    def _snapshot(self,c,project):
        rows=c.execute('''SELECT t.id,t.state,t.worker_subject,t.report,t.review,p.priority,p.due,f.plan,s.task AS stopped FROM bridge_tasks t
            LEFT JOIN bridge_task_planning p ON p.task=t.id LEFT JOIN bridge_failure_records f ON f.task=t.id
            LEFT JOIN bridge_stop_confirmations s ON s.task=t.id WHERE t.project=? ORDER BY t.id''',(project,)).fetchall()
        cards={}
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bridge_review_cards'").fetchone():
            for card in c.execute('''SELECT v.* FROM bridge_review_cards v WHERE revision=(
                SELECT MAX(revision) FROM bridge_review_cards WHERE task=v.task)''').fetchall():
                cards[card['task']]={'revision':card['revision'],'digest':card['package_digest'],
                                     'acknowledged':card['approval'] is not None}
        policies={row['task']:row['policy_digest'] for row in c.execute('SELECT task,policy_digest FROM bridge_task_policy')}
        snapshot=[]
        for r in rows:
            verified=None
            if r['report']:
                review=json.loads(r['review']) if r['review'] else {}
                verified=all(check['passed'] or (check['name']=='artifact_access_verified' and
                    review.get('evidence_reference','').startswith('independent-artifact:'))
                    for check in json.loads(r['report'])['checks'])
            snapshot.append({'id':r['id'],'state':r['state'],'priority':r['priority'] or 0,
                 'due_at':r['due'],'stop_unconfirmed':r['state']=='cancelled' and r['worker_subject'] is not None and not r['stopped'],
                 'overdue':bool(r['due'] and r['due']<self.store._now() and r['state'] not in ('reviewed','cancelled')),
                 'failure':json.loads(r['plan'])['category'] if r['plan'] and r['state'] not in ('reviewed','cancelled') else None,
                 'checks_passed':verified,'review_card':cards.get(r['id']),'policy_digest':policies.get(r['id'])})
        return snapshot

    @staticmethod
    def _recipient(c,actor):
        session=c.execute('SELECT parent_hash FROM bridge_web_sessions WHERE token_hash=?',(actor['token_hash'],)).fetchone()
        return session['parent_hash'] if session else actor['token_hash']

    def preview_digest(self,token,project):
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            self.store._project_access(c,actor,project);self.store._expire(c,project)
            snapshot=self._snapshot(c,project);fingerprint=digest(snapshot)
            previous=c.execute('SELECT * FROM bridge_digest_receipts WHERE project=? AND recipient=?',
                               (project,self._recipient(c,actor))).fetchone()
            old={item['id']:item for item in json.loads(previous['snapshot'])} if previous else {}
            changed=[item for item in snapshot if old.get(item['id'])!=item]
            return {'fingerprint':fingerprint,'notify':bool(changed),'changed':changed[:100],
                    'changed_count':len(changed),'truncated':len(changed)>100,'external_message_sent':False}

    def acknowledge_digest(self,token,project,fingerprint):
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            self.store._project_access(c,actor,project);self.store._expire(c,project)
            snapshot=self._snapshot(c,project)
            if digest(snapshot)!=fingerprint:
                raise Conflict('보고를 읽는 동안 상태가 바뀌어 다시 확인해야 해요.')
            c.execute('INSERT OR REPLACE INTO bridge_digest_receipts VALUES (?,?,?,?)',
                      (project,self._recipient(c,actor),fingerprint,canonical_json(snapshot)))
            return {'acknowledged':True,'external_message_sent':False}
