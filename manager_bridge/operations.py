"""Observed phase timings and opt-in backpressure. No execution or billing claims."""
import json

from .store import Conflict


class Operations:
    def __init__(self,store):
        self.store=store

    @staticmethod
    def create_tables(c):
        c.executescript('''
            CREATE TABLE IF NOT EXISTS bridge_capacity_limits (
                scope TEXT PRIMARY KEY,max_active INTEGER NOT NULL,max_review INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS bridge_verification_timings (
                task TEXT NOT NULL,report_digest TEXT NOT NULL,check_name TEXT NOT NULL,
                seconds REAL NOT NULL,recorded REAL NOT NULL,PRIMARY KEY(task,report_digest,check_name));
        ''')

    @staticmethod
    def capacity_blocked(c,project):
        # Uncertain/cancelled-but-unconfirmed executions still occupy capacity.
        for scope,filter_sql,args in (('*','',()),(project,' WHERE project=?',(project,))):
            policy=c.execute('SELECT * FROM bridge_capacity_limits WHERE scope=?',(scope,)).fetchone()
            if not policy:
                continue
            rows=c.execute('SELECT id,state,worker_subject FROM bridge_tasks'+filter_sql,args).fetchall()
            active=0
            for row in rows:
                unconfirmed=(row['state']=='cancelled' and row['worker_subject'] is not None and not c.execute(
                    'SELECT 1 FROM bridge_stop_confirmations WHERE task=?',(row['id'],)).fetchone())
                active+=row['state'] in ('running','needs_verification') or unconfirmed
            pending=sum(row['state']=='reported' for row in rows)
            if active>=policy['max_active'] or pending>=policy['max_review']:
                return True
        return False

    def set_capacity(self,token,project,*,max_active,max_review):
        if type(max_active) is not int or not 1<=max_active<=64 or type(max_review) is not int or not 1<=max_review<=1000:
            raise ValueError('실행/검토 대기 상한을 확인해 주세요.')
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token)
            if project is None:
                self.store._role(actor,('owner',))
                if actor['projects']!=['*']:
                    raise Conflict('서버 전체 상한은 전체 범위 소유자만 지정해요.')
                scope='*'
            else:
                self.store._role(actor,('owner','manager'))
                self.store._project_access(c,actor,project);scope=project
            c.execute('INSERT OR REPLACE INTO bridge_capacity_limits VALUES (?,?,?)',(scope,max_active,max_review))
            return {'project':project,'max_active':max_active,'max_review':max_review,'running_jobs_interrupted':False}

    def timings(self,token,task_id):
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            task=self.store._get(c,actor,task_id)
            events=c.execute('SELECT action,recorded FROM bridge_events WHERE task=? ORDER BY id',(task_id,)).fetchall()
            first={}
            for event in events:
                first.setdefault(event['action'],event['recorded'])
            first.setdefault('submitted',task['created'])
            now=self.store._now()
            def interval(start,end,active_states):
                if start not in first:return None
                finish=first.get(end)
                if finish is None and task['state'] in active_states:finish=now
                return round(max(0,finish-first[start]),3) if finish is not None else None
            checks=c.execute('SELECT check_name,seconds FROM bridge_verification_timings WHERE task=? ORDER BY recorded,check_name',
                             (task_id,)).fetchall()
            return {'task_id':task_id,'approval_wait_seconds':interval('submitted','approved',('pending_approval',)),
                'queue_wait_seconds':interval('approved','claimed',('queued',)),
                'observed_execution_wall_seconds':interval('claimed','worker_reported',('running',)),
                'review_wait_seconds':interval('worker_reported','manager_reviewed',('reported',)),
                'independent_checks':[dict(row) for row in checks],
                'unknown_execution_end':task['state'] in ('needs_verification','cancelled') and task['worker_subject'] is not None and not bool(c.execute('SELECT 1 FROM bridge_stop_confirmations WHERE task=?',(task_id,)).fetchone()),
                'cpu_or_model_usage_measured':False,'billing_estimate_provided':False}
