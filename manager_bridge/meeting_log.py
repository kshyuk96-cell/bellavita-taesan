"""Exact owner-confirmed meeting records and follow-through; no approval by prose."""
import json
import secrets
from .maintenance_registry import name
from .store import Conflict, NotFound, _reject_credentials, _text, canonical_json, digest


class Meetings:
    def __init__(self,store,registry):
        self.store,self.registry=store,registry
        with store._transaction() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS management_meetings (
              id TEXT PRIMARY KEY,project TEXT,request_key TEXT,body TEXT,
              fingerprint TEXT,state TEXT,confirmation TEXT,created REAL,
              UNIQUE(project,request_key))''')

    def prepare(self,token,project,request_key,*,agenda,decisions,task_ids):
        name(request_key);_text(agenda,500,'meeting agenda')
        if type(decisions) is not list or not 1<=len(decisions)<=30:
            raise ValueError('bounded meeting decisions required')
        for decision in decisions:
            if type(decision) is not dict or set(decision)!={'kind','summary'} or decision['kind'] not in ('develop','review','question','hold'):
                raise ValueError('structured meeting decision required')
            _text(decision['summary'],1000,'decision summary')
        if type(task_ids) is not list or len(task_ids)>100 or len(set(task_ids))!=len(task_ids):
            raise ValueError('bounded distinct task bindings required')
        _reject_credentials({'agenda':agenda,'decisions':decisions,'key':request_key},token)
        with self.store._transaction() as c:
            config=self.registry._get(c,token,project)
            actor=self.store._authenticate(c,token)
            tasks=[]
            for key in task_ids:
                task=self.store._get(c,actor,key)
                if task['project']!=project:raise Conflict('meeting includes a different project')
                tasks.append({'id':key,'version':task['version'],'manifest_digest':task['manifest_digest']})
            body={'project':project,'config_fingerprint':config['fingerprint'],'agenda':agenda,
                  'decisions':decisions,'tasks':tasks}
            fingerprint=digest(body)
            old=c.execute('SELECT * FROM management_meetings WHERE project=? AND request_key=?',(project,request_key)).fetchone()
            if old:
                if old['fingerprint']!=fingerprint:raise Conflict('meeting key reused for changed decisions')
                return self._public(old)
            key=secrets.token_hex(16)
            c.execute('INSERT INTO management_meetings VALUES (?,?,?,?,?,?,NULL,?)',
                      (key,project,request_key,canonical_json(body),fingerprint,'proposed',self.store._now()))
            return self._public(c.execute('SELECT * FROM management_meetings WHERE id=?',(key,)).fetchone())

    @staticmethod
    def _public(row):
        return {'id':row['id'],'project':row['project'],'fingerprint':row['fingerprint'],
                'state':row['state'],'record':json.loads(row['body']),
                'task_approval_granted':False,'operational_approval':False}

    def _row(self,c,token,key,*,owner=False):
        name(key);row=c.execute('SELECT * FROM management_meetings WHERE id=?',(key,)).fetchone()
        if row is None:raise NotFound('meeting not found')
        self.registry._get(c,token,row['project'],('owner',) if owner else ('owner','manager'))
        return row

    def confirm(self,token,key,fingerprint):
        with self.store._transaction() as c:
            row=self._row(c,token,key,owner=True)
            if row['fingerprint']!=fingerprint:raise Conflict('exact meeting record required')
            body=json.loads(row['body']);config=self.registry._get(c,token,row['project'],('owner',))
            if body['config_fingerprint']!=config['fingerprint']:raise Conflict('project changed after meeting proposal')
            actor=self.store._authenticate(c,token)
            for old in body['tasks']:
                task=self.store._get(c,actor,old['id'])
                if (task['version'],task['manifest_digest'])!=(old['version'],old['manifest_digest']):
                    raise Conflict('task changed after meeting proposal')
            c.execute("UPDATE management_meetings SET state='confirmed',confirmation=? WHERE id=?",(actor['subject'],key))
            return self._public(c.execute('SELECT * FROM management_meetings WHERE id=?',(key,)).fetchone())

    def follow_up(self,token,key):
        with self.store._transaction() as c:
            row=self._row(c,token,key)
            body=json.loads(row['body']);actor=self.store._authenticate(c,token)
            tasks=[]
            for bound in body['tasks']:
                task=self.store._get(c,actor,bound['id'])
                unchanged=(task['version'],task['manifest_digest'])==(bound['version'],bound['manifest_digest'])
                facts=c.execute('SELECT * FROM bridge_verified_candidates WHERE task=?',(task['id'],)).fetchone()
                report=json.loads(task['report']) if task['report'] else None
                independently_verified=bool(unchanged and task['state']=='reviewed' and report and facts and
                    facts['report_digest']==digest(report) and facts['head_sha']==report['head_sha'] and not facts['dirty'])
                tasks.append({'task_id':task['id'],'state':task['state'] if unchanged else 'changed',
                              'independently_verified':independently_verified})
            current=self.registry._get(c,token,row['project'])
            return {'meeting_id':key,'state':row['state'],'tasks':tasks,
                    'project_changed':body['config_fingerprint']!=current['fingerprint'],
                    'execution_started':False,'operational_change':False}
