"""Global-owner backup freshness, bounded readback and non-deleting retention.

RTO measures local quarantined restoration only. Backup completion age is not
actual customer data loss/RPO, and no provider is called without registration.
"""
from dataclasses import dataclass,asdict
import hashlib
import json
import math
from pathlib import Path
import tempfile
import time
from .encrypted_backup import EncryptedBackup,MAX_ENVELOPE_BYTES
from .governance_durability import _directory
from .maintenance_registry import name
from .store import Conflict,canonical_json,digest


@dataclass(frozen=True)
class BackupHealthPolicy:
    maximum_completion_age_seconds:int=86400
    retain_latest:int=7
    def __post_init__(self):
        if type(self.maximum_completion_age_seconds) is not int or not 1<=self.maximum_completion_age_seconds<=31536000 or type(self.retain_latest) is not int or not 1<=self.retain_latest<=100:
            raise ValueError('bounded backup health policy required')


class BackupHealth:
    def __init__(self,encrypted,*,rehearsal_root=None):
        if type(encrypted) is not EncryptedBackup:raise ValueError('registered encrypted backup required')
        self.encrypted,self.store=encrypted,encrypted.store
        self.core=encrypted.durability.core
        self.rehearsal_root=_directory(rehearsal_root) if rehearsal_root is not None else None
        with self.store._transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS backup_health_policy (singleton INTEGER PRIMARY KEY,revision INTEGER,body TEXT,digest TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS backup_health_rehearsals (request_key TEXT PRIMARY KEY,backup_id TEXT,state TEXT,result TEXT)')

    def install(self,owner,policy,*,expected_revision=0):
        self.encrypted._owner(owner)
        if type(policy) is not BackupHealthPolicy or type(expected_revision) is not int or expected_revision<0:raise ValueError('typed backup policy and exact revision required')
        with self.store._transaction() as c:
            self.store._authenticate(c,owner)
            old=c.execute('SELECT revision FROM backup_health_policy WHERE singleton=1').fetchone()
            if expected_revision!=(old['revision'] if old else 0):raise Conflict('backup health policy revision changed')
            revision=expected_revision+1;body=asdict(policy);stamp=digest({'revision':revision,'body':body})
            c.execute('INSERT OR REPLACE INTO backup_health_policy VALUES (1,?,?,?)',(revision,canonical_json(body),stamp))
            return {'revision':revision,'policy':body,'physical_deletion_performed':False}

    def _rows(self,c):
        return [dict(r) for r in c.execute('SELECT backup_id,state,ciphertext_digest,bytes,updated FROM encrypted_backups ORDER BY updated DESC,backup_id DESC LIMIT 101')]

    def inspect(self,owner,*,verify_latest=False):
        self.encrypted._owner(owner)
        if type(verify_latest) is not bool:raise ValueError('explicit bounded readback selection required')
        with self.store._transaction() as c:
            self.store._authenticate(c,owner);rows=self._rows(c)
            policy=c.execute('SELECT * FROM backup_health_policy WHERE singleton=1').fetchone()
            policy=dict(policy) if policy else None
            rehearsals=[dict(r) for r in c.execute('SELECT request_key,backup_id,state,result FROM backup_health_rehearsals ORDER BY request_key LIMIT 101')]
        current=[r for r in rows[:100] if r['state']=='stored'];latest=current[0] if current else None
        verified=False;verification='not_requested'
        if verify_latest and latest:
            try:
                data=self.encrypted.backend.get(latest['backup_id'],MAX_ENVELOPE_BYTES)
                verified=type(data) is bytes and len(data)==latest['bytes'] and hashlib.sha256(data).hexdigest()==latest['ciphertext_digest']
                verification='exact_ciphertext_readback' if verified else 'integrity_mismatch'
            except Exception:verification='unavailable'
            self.encrypted._owner(owner)
        elif verify_latest:verification='no_stored_backup'
        with self.store._transaction() as c:
            self.store._authenticate(c,owner)
            new_policy=c.execute('SELECT * FROM backup_health_policy WHERE singleton=1').fetchone()
            if self._rows(c)!=rows or (dict(new_policy) if new_policy else None)!=policy or [dict(r) for r in c.execute('SELECT request_key,backup_id,state,result FROM backup_health_rehearsals ORDER BY request_key LIMIT 101')]!=rehearsals:raise Conflict('backup health binding changed during readback')
            now=self.store._now()
            if type(now) not in (int,float) or not math.isfinite(now) or now<=0:raise Conflict('finite backup observation clock required')
            valid_latest=latest and type(latest['updated']) in (int,float) and math.isfinite(latest['updated']) and 0<=now-latest['updated']
            age=now-latest['updated'] if valid_latest else None
            body=BackupHealthPolicy(**json.loads(policy['body'])) if policy else None
            if policy and policy['digest']!=digest({'revision':policy['revision'],'body':asdict(body)}):raise Conflict('backup health policy integrity mismatch')
            reason='policy_missing' if body is None else 'no_stored_backup' if latest is None else 'backup_clock_unverified' if age is None else 'backup_completion_stale' if age>body.maximum_completion_age_seconds else None
            uncertain=sum(r['state']!='stored' for r in rows[:100])
            if reason is None and uncertain:reason='backup_uncertainty_requires_reconciliation'
            if reason is None and verify_latest and not verified:reason='backup_readback_unverified'
            unresolved_rehearsals=sum(r['state']!='complete' for r in rehearsals[:100])
            if reason is None and unresolved_rehearsals:reason='restore_rehearsal_unresolved'
            result={'state':'observed_current' if reason is None else 'attention_required','reason':reason,
                'latest_stored_backup':latest['backup_id'] if latest else None,'completion_age_seconds':age,
                'readback_state':verification,'exact_readback_verified':verified,'uncertain_count_in_window':uncertain,
                'unresolved_rehearsals_in_window':unresolved_rehearsals,'rehearsal_window_partial':len(rehearsals)>100,
                'window':{'limit':100,'partial':len(rows)>100},
                'retention_candidates':[r['backup_id'] for r in current[body.retain_latest:]] if body else [],
                'physical_deletion_performed':False,'offsite_verified':False,'actual_customer_data_loss_seconds':None,
                'customer_rpo_verified':False,'observed_at':now,'execution_authorized':False}
            if len(rows)>100 or len(rehearsals)>100:result.update(state='attention_required',reason='backup_inventory_partial')
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c,result)
            return result

    def rehearse(self,owner,backup_id,request_key):
        self.encrypted._owner(owner);name(backup_id);name(request_key)
        if self.rehearsal_root is None:raise Conflict('private rehearsal root not configured')
        with self.store._transaction() as c:
            self.store._authenticate(c,owner)
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c,{'backup_id':backup_id,'request_key':request_key})
            if c.execute('SELECT 1 FROM backup_health_rehearsals WHERE request_key=?',(request_key,)).fetchone():raise Conflict('rehearsal already attempted; inspect outcome instead of replay')
            c.execute("INSERT INTO backup_health_rehearsals VALUES (?,?,'started',NULL)",(request_key,backup_id))
        started=time.monotonic()
        try:
            _directory(self.rehearsal_root)
            with tempfile.TemporaryDirectory(dir=self.rehearsal_root,prefix='benchmark-') as directory:
                restored=self.encrypted.restore_rehearsal(owner,backup_id,Path(directory)/'recovered.sqlite3')
            if restored.get('reconciliation_required') is not True:raise Conflict('quarantined rehearsal required')
            elapsed=time.monotonic()-started
            self.encrypted._owner(owner)
            result={'backup_id':backup_id,'state':'local_quarantined_rehearsal_complete','elapsed_seconds':elapsed,
                'quarantined':restored['reconciliation_required'] is True,'actual_customer_data_loss_seconds':None,
                'customer_rpo_verified':False,'production_rto_verified':False,'offsite_verified':False,'automatic_replay':False}
            with self.store._transaction() as c:
                self.store._authenticate(c,owner)
                c.execute("UPDATE backup_health_rehearsals SET state='complete',result=? WHERE request_key=?",(canonical_json(result),request_key))
            return result
        except BaseException:
            with self.store._transaction() as c:c.execute("UPDATE backup_health_rehearsals SET state='uncertain' WHERE request_key=?",(request_key,))
            raise
