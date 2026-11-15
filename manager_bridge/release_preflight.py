"""Read-only source/live/candidate preflight. It never authorizes or deploys."""
import json
import math
import re

from .store import digest, _sha


class ReleasePreflight:
    def __init__(self,store,*,repository_observer=None,service_observer=None):
        self.store,self.repository_observer,self.service_observer=store,repository_observer,service_observer
        with store._transaction() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS bridge_release_preflights (
                task TEXT PRIMARY KEY,report TEXT NOT NULL,fingerprint TEXT NOT NULL,recorded REAL NOT NULL)''')

    def _provenance(self,value,token):
        return (isinstance(value,dict) and isinstance(value.get('receipt'),str)
            and re.fullmatch(r'[A-Za-z0-9_:/#.-]{1,300}',value['receipt']) and token not in value['receipt']
            and type(value.get('observed_at')) in (int,float) and math.isfinite(value['observed_at'])
            and -15<=self.store._now()-value['observed_at']<=300)

    def run(self,token,task_id,*,rollback_sha):
        _sha(rollback_sha)
        actor=self.store.authenticate(token)
        with self.store._transaction() as c:
            self.store._role(actor,('owner','manager'))
            task=self.store._get(c,actor,task_id)
            facts=c.execute('SELECT * FROM bridge_verified_candidates WHERE task=?',(task_id,)).fetchone()
            report=json.loads(task['report']) if task['report'] else None
            candidate=report['head_sha'] if report else json.loads(task['manifest'])['base_sha']
            valid=(task['state']=='reviewed' and facts and not facts['dirty'] and report
                and facts['report_digest']==digest(report) and facts['head_sha']==candidate)
            review=json.loads(task['review']) if task['review'] else {}
            if report and any(not check['passed'] and not (check['name']=='artifact_access_verified' and
                review.get('evidence_reference','').startswith('independent-artifact:')) for check in report['checks']):
                valid=False
            repo=json.loads(task['manifest'])['repository'];project=task['project']
        issues=[] if valid else ['candidate_independent_verification_required']
        snapshots={}
        for name,observer in (('repository',self.repository_observer),('service',self.service_observer)):
            if not callable(observer):issues.append(name+'_connection_required');continue
            try:
                value=observer(project,repo,candidate,rollback_sha)
                if not self._provenance(value,token):raise ValueError('unverified provenance')
                if name=='repository':
                    if set(value)!={'repository','main_sha','candidate_sha','rollback_sha','candidate_contains_main','rollback_exists','receipt','observed_at'}:
                        raise ValueError('invalid repository snapshot')
                    for field in ('main_sha','candidate_sha','rollback_sha'):_sha(value[field])
                    if type(value['candidate_contains_main']) is not bool or type(value['rollback_exists']) is not bool:
                        raise ValueError('actual boolean required')
                    if value['candidate_sha']!=candidate or value['rollback_sha']!=rollback_sha or value['repository']!=repo:
                        raise ValueError('source binding mismatch')
                else:
                    if set(value)!={'repository','branch','live_sha','status','deploy_id','health_ready','auto_deploy','disk_attached','receipt','observed_at'}:
                        raise ValueError('invalid service snapshot')
                    _sha(value['live_sha'])
                    if (value['repository']!=repo or value['branch']!='main' or
                        value['status'] not in ('live','pending','failed') or
                        not isinstance(value['deploy_id'],str) or not re.fullmatch(r'dep-[A-Za-z0-9_-]{1,100}',value['deploy_id']) or
                        any(type(value[key]) is not bool for key in ('health_ready','auto_deploy','disk_attached'))):
                        raise ValueError('unverified service binding')
                snapshots[name]=value
            except Exception:
                issues.append(name+'_observation_unverified')
        source=snapshots.get('repository');live=snapshots.get('service')
        if source:
            if not source['candidate_contains_main']:issues.append('latest_main_not_integrated')
            if not source['rollback_exists']:issues.append('rollback_commit_missing')
        if live:
            if live['status']!='live' or not live['health_ready']:issues.append('live_health_not_verified')
        if source and live:
            if source['main_sha']!=live['live_sha']:issues.append('main_live_sha_mismatch')
            if rollback_sha!=live['live_sha']:issues.append('rollback_not_current_live')
        result={'task_id':task_id,'candidate_sha':candidate,'rollback_sha':rollback_sha,
            'status':'blocked' if issues else 'ready_for_owner_review','issues':issues,'observations':snapshots,
            'checked_at':self.store._now(),'operational_approval_recorded':False,'deployment_executed':False,
            'disk_downtime_review_required':bool(live and live['disk_attached']),
            'external_data_rollback_verified':False}
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            current=self.store._get(c,actor,task_id)
            if current['state']!=task['state'] or current['report']!=task['report']:
                raise ValueError('점검 중 검토 대상이 바뀌었어요.')
            c.execute('INSERT OR REPLACE INTO bridge_release_preflights VALUES (?,?,?,?)',
                (task_id,json.dumps(result,sort_keys=True),digest(result),self.store._now()))
        return result

    def latest(self,token,task_id):
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            self.store._get(c,actor,task_id)
            row=c.execute('SELECT report,recorded FROM bridge_release_preflights WHERE task=?',(task_id,)).fetchone()
            if not row:return {'task_id':task_id,'status':'not_checked','deployment_executed':False}
            return {**json.loads(row['report']),'stale':self.store._now()-row['recorded']>300}
