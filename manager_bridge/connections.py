"""Read-only integration checklist from trusted registered observations.

This module does not search for credentials, invoke task-provided commands,
change permissions, or equate permission denied with empty business data.
"""
import json
import re
from dataclasses import dataclass

from .store import digest


@dataclass(frozen=True)
class Probe:
    observe: object
    read_only: bool=True
    def __post_init__(self):
        if not callable(self.observe) or self.read_only is not True:
            raise ValueError('등록된 읽기 전용 연결 확인만 사용할 수 있어요.')


class Connections:
    EXPECTED=('erp','kakao_dev','sheet_copy','company_pc','asset_originals','native_development','codex_cli','render')
    def __init__(self,store,probes=None):
        self.store,self.probes=store,dict(probes or {})
        if set(self.probes)-set(self.EXPECTED) or any(not isinstance(p,Probe) for p in self.probes.values()):
            raise ValueError('정해진 연결 점검기를 등록해 주세요.')
        with store._transaction() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS bridge_connection_reports (
                project TEXT PRIMARY KEY,report TEXT NOT NULL,fingerprint TEXT NOT NULL,recorded REAL NOT NULL)''')

    def inspect(self,token,project):
        actor=self.store.authenticate(token)
        with self.store._transaction() as c:
            self.store._role(actor,('owner','manager'));self.store._project_access(c,actor,project)
        results=[]
        for name in self.EXPECTED:
            if name not in self.probes:
                results.append({'name':name,'state':'not_configured','evidence_reference':None})
                continue
            try:
                observed=self.probes[name].observe(project)
                if (not isinstance(observed,dict) or set(observed)!={'state','evidence_reference'}
                        or observed['state'] not in ('ready','unavailable','permission_denied','unknown')
                        or not isinstance(observed['evidence_reference'],str)
                        or not re.fullmatch(r'[A-Za-z0-9_:/#.-]{1,300}',observed['evidence_reference'])
                        or token in observed['evidence_reference']):
                    raise ValueError('unverified observation')
                results.append({'name':name,**observed})
            except Exception:
                results.append({'name':name,'state':'unknown','evidence_reference':None})
        report={'project':project,'connections':results,'checked_at':self.store._now(),
                'scope':'registered_read_only_probes','configuration_changed':False}
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            self.store._project_access(c,actor,project)
            c.execute('INSERT OR REPLACE INTO bridge_connection_reports VALUES (?,?,?,?)',
                      (project,json.dumps(report,sort_keys=True),digest(report),self.store._now()))
        return report

    def latest(self,token,project,*,max_age=300):
        if type(max_age) is not int or not 1<=max_age<=3600:raise ValueError('점검 유효기간이 필요해요.')
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            self.store._project_access(c,actor,project)
            row=c.execute('SELECT * FROM bridge_connection_reports WHERE project=?',(project,)).fetchone()
            if not row:return {'project':project,'state':'not_checked','connections':[]}
            return {**json.loads(row['report']),'stale':self.store._now()-row['recorded']>max_age}
