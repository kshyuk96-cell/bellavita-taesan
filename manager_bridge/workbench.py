"""Manager dashboard and immutable review cards; never executes deployments."""
import json
import re

from .coordination import Coordination
from .store import Conflict, canonical_json, digest, _text, _sha


class Workbench:
    def __init__(self, store):
        self.store = store
        with store._transaction() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS bridge_review_cards (
                task TEXT NOT NULL,revision INTEGER NOT NULL,package TEXT NOT NULL,
                package_digest TEXT NOT NULL,approval TEXT,created REAL NOT NULL,
                PRIMARY KEY(task,revision))''')

    def projects(self, token):
        with self.store._transaction() as c:
            actor = self.store._authenticate(c, token)
            self.store._role(actor, ('owner', 'manager'))
            rows = c.execute('SELECT project,repository FROM bridge_projects ORDER BY project').fetchall()
            return [dict(row) for row in rows if self.store._scope(actor, row['project'])]

    def dashboard(self, token, project):
        # Overview performs authentication, scope validation and lease expiry.
        overview = self.store.overview(token, project)
        with self.store._transaction() as c:
            actor = self.store._authenticate(c, token)
            self.store._role(actor, ('owner', 'manager'))
            self.store._project_access(c, actor, project)
            rows = c.execute('''SELECT t.id,t.state,t.worker_subject,t.updated,t.manifest,p.priority,p.due FROM bridge_tasks t
                LEFT JOIN bridge_task_planning p ON p.task=t.id WHERE t.project=? ORDER BY t.updated DESC,t.id LIMIT 101''', (project,)).fetchall()
            tasks = [{'id': row['id'], 'state': row['state'], 'worker': row['worker_subject'],
                      'kind': json.loads(row['manifest'])['kind'], 'updated_at': row['updated'],
                      'priority':row['priority'] or 0,'due_at':row['due'],
                      'overdue':bool(row['due'] and row['due']<self.store._now() and row['state'] not in ('reviewed','cancelled'))} for row in rows[:100]]
        return {'project': project, **overview, 'tasks': tasks, 'tasks_truncated': len(rows)>100,
                'connection_scope': 'this_registered_manager_server', 'background_execution_guaranteed': False}

    def card(self, token, task_id):
        with self.store._transaction() as c:
            actor = self.store._authenticate(c, token)
            self.store._role(actor, ('owner', 'manager'))
            row = self.store._get(c, actor, task_id)
            package = c.execute('SELECT * FROM bridge_review_cards WHERE task=? ORDER BY revision DESC LIMIT 1',
                                (task_id,)).fetchone()
            verified = c.execute('SELECT * FROM bridge_verified_candidates WHERE task=?', (task_id,)).fetchone()
        task = self.store.get_task(token, task_id)
        policy = Coordination(self.store).get_policy(token, task_id)
        return {'task_id': task_id, 'state': task['state'], 'version': task['version'],
                'manifest_digest': task['manifest_digest'], 'policy_digest': digest(policy) if policy else None,
                'project': task['project'], 'repository': task['manifest']['repository'],
                'base_sha': task['manifest']['base_sha'], 'request': task['manifest']['description'],
                'checks': task['report']['checks'] if task['report'] else [], 'policy': policy,
                'can_approve_development': actor['role']=='owner' and task['state']=='pending_approval',
                'review_package': json.loads(package['package']) if package else None,
                'package_revision': package['revision'] if package else None,
                'package_digest': package['package_digest'] if package else None,
                'review_acknowledgement': json.loads(package['approval']) if package and package['approval'] else None,
                'can_acknowledge': actor['role']=='owner' and task['state']=='reviewed' and bool(package) and bool(verified)
                    and not verified['dirty'] and verified['head_sha']==json.loads(package['package'])['details']['candidate_sha']
                    and verified['report_digest']==json.loads(package['package'])['report_digest'],
                'deployment_executed': False}

    def put_package(self, token, task_id, package):
        required = {'summary','impact','risks','rollback_sha','candidate_sha','pr_url','evidence_references'}
        if not isinstance(package, dict) or set(package)!=required:
            raise ValueError('변경·영향·위험·후보·복구·PR·검증 근거를 지정해 주세요.')
        for key in ('summary','impact','risks'):
            _text(package[key],1000,'검토 요약')
        _sha(package['candidate_sha']); _sha(package['rollback_sha'])
        refs=package['evidence_references']
        if (not isinstance(refs,list) or not 1<=len(refs)<=30 or any(
                not isinstance(ref,str) or not re.fullmatch(r'[A-Za-z0-9_:/#.-]{1,500}',ref) for ref in refs)):
            raise ValueError('검증 결과 원문 대신 근거 참조를 지정해 주세요.')
        if token in canonical_json(package):
            raise ValueError('인증 정보는 검토안에 포함할 수 없어요.')
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token)
            self.store._role(actor,('owner','manager'))
            row=self.store._get(c,actor,task_id)
            if row['state']!='reviewed':
                raise Conflict('독립 검토 기록이 있는 작업만 최종 검토안으로 묶을 수 있어요.')
            report=json.loads(row['report'])
            review=json.loads(row['review'])
            if any(not check['passed'] and not (check['name']=='artifact_access_verified' and
                   review['evidence_reference'].startswith('independent-artifact:')) for check in report['checks']):
                raise Conflict('실패한 검증이 남은 작업은 최종 검토 카드로 올리지 않아요.')
            verified=c.execute('SELECT * FROM bridge_verified_candidates WHERE task=?',(task_id,)).fetchone()
            if (not verified or verified['dirty'] or verified['report_digest']!=digest(report)
                    or verified['head_sha']!=package['candidate_sha'] or report['head_sha']!=package['candidate_sha']):
                raise Conflict('정확한 후보 커밋의 깨끗한 작업폴더를 독립 검증한 근거가 필요해요.')
            repo=json.loads(row['manifest'])['repository']
            if not isinstance(package['pr_url'],str) or not re.fullmatch(r'https://github\.com/'+re.escape(repo)+r'/pull/[1-9][0-9]*',package['pr_url']):
                raise ValueError('대상 저장소의 정확한 PR 주소가 필요해요.')
            bound={'task_id':task_id,'version':row['version'],'manifest_digest':row['manifest_digest'],
                   'report_digest':digest(json.loads(row['report'])),'details':package,
                   'scope':'review_acknowledgement_only','deployment_executed':False}
            fingerprint=digest(bound)
            old=c.execute('SELECT * FROM bridge_review_cards WHERE task=? ORDER BY revision DESC LIMIT 1',(task_id,)).fetchone()
            if old and old['package_digest']==fingerprint:
                return {'revision':old['revision'],'package_digest':fingerprint}
            revision=old['revision']+1 if old else 1
            c.execute('INSERT INTO bridge_review_cards VALUES (?,?,?,?,NULL,?)',
                      (task_id,revision,canonical_json(bound),fingerprint,self.store._now()))
            self.store._event(c,task_id,actor['subject'],'review_card_created','reviewed',bound)
            return {'revision':revision,'package_digest':fingerprint}

    def acknowledge(self,token,task_id,revision,package_digest):
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token)
            self.store._role(actor,('owner',))
            task=self.store._get(c,actor,task_id)
            row=c.execute('SELECT * FROM bridge_review_cards WHERE task=? ORDER BY revision DESC LIMIT 1',(task_id,)).fetchone()
            if (task['state']!='reviewed' or not row or type(revision) is not int or revision!=row['revision']
                    or package_digest!=row['package_digest']):
                raise Conflict('현재 후보 커밋에 묶인 정확한 검토 카드를 확인해 주세요.')
            bound=json.loads(row['package'])
            verified=c.execute('SELECT * FROM bridge_verified_candidates WHERE task=?',(task_id,)).fetchone()
            if (not verified or verified['dirty'] or verified['report_digest']!=bound['report_digest']
                    or verified['head_sha']!=bound['details']['candidate_sha']):
                raise Conflict('현재 검토 카드의 후보 검증 근거가 없거나 달라요.')
            if bound['report_digest']!=digest(json.loads(task['report'])):
                raise Conflict('검토 대상 보고가 바뀌었어요.')
            if row['approval']:
                return json.loads(row['approval'])
            approval={'actor':actor['subject'],'candidate_sha':bound['details']['candidate_sha'],
                      'package_digest':package_digest,'revision':revision,'approved_at':self.store._now(),
                      'scope':'review_acknowledgement_only','deployment_executed':False}
            c.execute('UPDATE bridge_review_cards SET approval=? WHERE task=? AND revision=?',
                      (canonical_json(approval),task_id,revision))
            self.store._event(c,task_id,actor['subject'],'review_card_acknowledged','reviewed',approval)
            return approval
