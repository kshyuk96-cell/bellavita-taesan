"""Single authenticated manager intake. Routing is deterministic, not NLP scoring.

Original text/media is not persisted. Text (including quoted approval language)
never grants authority. Only direct host-authenticated requests may create tasks;
actual execution remains subject to owner/delegation approval and registered workers.
"""
import json
import secrets
from .maintenance_registry import name
from .store import Conflict, NotFound, PermissionDenied, _manifest, _reject_credentials, canonical_json, digest


class Intake:
    def __init__(self,store,registry,*,aliases=None,context_ttl=1200):
        if registry.store is not store:raise ValueError('shared manager registry required')
        if type(context_ttl) is not int or not 60<=context_ttl<=3600:raise ValueError('bounded conversation lifetime required')
        self.store,self.registry,self.ttl=store,registry,context_ttl
        self.aliases={}
        for label,projects in (aliases or {}).items():
            if type(label) is not str or not label.strip() or len(label)>100 or type(projects) not in (tuple,list) or not projects:
                raise ValueError('trusted project aliases required')
            self.aliases[label]=tuple(name(p) for p in projects)
        with store._transaction() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS management_intake (
              id TEXT PRIMARY KEY,actor TEXT,request_key TEXT,conversation TEXT,
              fingerprint TEXT,project TEXT,source TEXT,state TEXT,task TEXT,
              created REAL,UNIQUE(actor,request_key));
            CREATE TABLE IF NOT EXISTS management_context (
              actor TEXT,conversation TEXT,project TEXT,expires REAL,
              PRIMARY KEY(actor,conversation));
            ''')

    def _actor(self,c,token):
        actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
        return actor

    def receive(self,token,request_key,conversation,text,*,source='direct_request',project_hint=None,follow_up=False):
        name(request_key);name(conversation)
        if type(text) is not str or not text.strip() or len(text)>8000 or type(follow_up) is not bool:
            raise ValueError('bounded current request required')
        if source not in ('direct_request','business_material'):raise ValueError('trusted origin classification required')
        _reject_credentials({'text':text,'key':request_key,'conversation':conversation,'project':project_hint},token)
        accessible={x['project']:x for x in self.registry.list(token)}
        labels={key:(key,) for key in accessible}
        for project,value in accessible.items():labels[value['config']['name']]=(project,)
        labels.update(self.aliases)
        candidates=set()
        for label,projects in labels.items():
            if label.casefold() in text.casefold():candidates.update(p for p in projects if p in accessible)
        if project_hint is not None:
            name(project_hint)
            if project_hint not in accessible:raise NotFound('project not available to this manager')
        fingerprint=digest({'conversation':conversation,'text_digest':digest(text),'source':source,
                            'hint':project_hint,'follow_up':follow_up})
        with self.store._transaction() as c:
            actor=self._actor(c,token)
            old=c.execute('SELECT * FROM management_intake WHERE actor=? AND request_key=?',(actor['token_hash'],request_key)).fetchone()
            if old:
                if old['fingerprint']!=fingerprint:raise Conflict('intake key reused for changed request')
                return self._public(self._row(c,token,old['id']))
            if source=='business_material':
                c.execute('DELETE FROM management_context WHERE actor=? AND conversation=?',(actor['token_hash'],conversation))
            selected=None;state='needs_project'
            if project_hint is not None:
                if candidates and candidates!={project_hint}:state='project_conflict'
                else:selected=project_hint
            elif len(candidates)==1:selected=next(iter(candidates))
            elif not candidates and follow_up and source=='direct_request':
                previous=c.execute('SELECT project,expires FROM management_context WHERE actor=? AND conversation=?',(actor['token_hash'],conversation)).fetchone()
                if previous and previous['expires']>self.store._now() and previous['project'] in accessible:
                    selected=previous['project']
            if selected:
                self.registry._get(c,token,selected)
                state='received' if source=='direct_request' else 'material_received'
                if source=='direct_request':
                    c.execute('INSERT OR REPLACE INTO management_context VALUES (?,?,?,?)',
                              (actor['token_hash'],conversation,selected,self.store._now()+self.ttl))
            else:
                c.execute('DELETE FROM management_context WHERE actor=? AND conversation=?',(actor['token_hash'],conversation))
            key=secrets.token_hex(16)
            c.execute('INSERT INTO management_intake VALUES (?,?,?,?,?,?,?,?,NULL,?)',
                      (key,actor['token_hash'],request_key,conversation,fingerprint,selected,source,state,self.store._now()))
            return self._public(c.execute('SELECT * FROM management_intake WHERE id=?',(key,)).fetchone())

    @staticmethod
    def _public(row):
        return {'id':row['id'],'project':row['project'],'state':row['state'],'source':row['source'],
                'task_id':row['task'],'fingerprint':row['fingerprint'],
                'approval_granted':False,'original_text_stored':False}

    def _row(self,c,token,key):
        name(key);actor=self._actor(c,token)
        row=c.execute('SELECT * FROM management_intake WHERE id=?',(key,)).fetchone()
        if row is None:raise NotFound('intake not found')
        if row['project'] is None:
            if row['actor']!=actor['token_hash']:raise NotFound('unresolved intake is private to its origin')
        else:self.registry._get(c,token,row['project'])
        return row

    def get(self,token,key):
        with self.store._transaction() as c:return self._public(self._row(c,token,key))

    def submit_task(self,token,key,manifest):
        """Structured manager proposal, never free-text execution or implicit approval."""
        manifest=_manifest(manifest)
        with self.store._transaction() as c:
            row=self._row(c,token,key)
            if row['source']!='direct_request' or row['state'] not in ('received','task_proposed') or not row['project']:
                raise PermissionDenied('resolved direct request required')
            if manifest['project']!=row['project']:raise Conflict('task differs from requested project')
            if row['task']:
                actor=self._actor(c,token);task=self.store._get(c,actor,row['task'])
                if task['manifest_digest']!=digest(manifest):raise Conflict('intake already has a different task')
                return self.store._public(task)
        task=self.store.submit(token,'intake-'+key,manifest)
        with self.store._transaction() as c:
            row=self._row(c,token,key)
            if row['task'] and row['task']!=task['id']:raise Conflict('intake task changed')
            c.execute("UPDATE management_intake SET task=?,state='task_proposed' WHERE id=?",(task['id'],key))
        return task
