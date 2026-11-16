"""Single-attempt QA worker; trusted registry, isolated child, fenced manager writes."""
import threading
from .control import request, validate_url
from .qa_runtime import QASandbox, QAInterrupted
from .store import Conflict, digest


class QAClient:
    def __init__(self, base, token):
        self.base, self.token = validate_url(base), token

    def call(self, method, job, action=None, payload=None):
        import re
        if not re.fullmatch('[a-f0-9]{32}', job):
            raise ValueError('QA job id required')
        path = '/v1/qa/jobs/'+job+('/'+action if action else '')
        try:
            return request(self.base, self.token, method, path, payload,
                           max_response_bytes=40*1024*1024)
        except Exception:
            return {'ok':False,'error':'connection_unverified','retry_performed':False}


class QAWorker:
    def __init__(self, client, registry, *, engine=None, stop=None):
        self.client, self.registry = client, registry
        self.engine = engine if engine is not None else QASandbox()
        self.stop = stop if stop is not None else threading.Event()

    def _uncertain(self, job, reason):
        response = self.client.call('POST',job,'uncertain',dict(reason=reason))
        return dict(state='reconciliation_required', uncertainty_recorded=bool(response.get('ok')),
                    retry_performed=False, operational_authorization=False)

    def run_once(self, job):
        inspected = self.client.call('GET',job,'execution')
        if not inspected.get('ok'):
            return dict(state='connection_unverified',retry_performed=False)
        execution = inspected['execution']
        if execution['job']['state']=='running':
            return self._uncertain(job,'worker_restarted')
        if execution['job']['state']!='queued':
            return dict(state=execution['job']['state'],retry_performed=False)
        profile = self.registry.resolve(execution['manifest'])
        status = self.engine.probe()
        if status.get('isolation_verified') is not True or status.get('available') is not True:
            return dict(state='sandbox_unavailable',execution_started=False,retry_performed=False)
        if self.stop.is_set():
            return dict(state='stopped',execution_started=False,retry_performed=False)
        claimed = self.client.call('POST',job,'claim',{})
        if not claimed.get('ok'):
            # A lost claim response might have committed. Never replay the claim.
            return dict(state='claim_uncertain',retry_performed=False)
        claim = claimed['job']
        manifest = claim['manifest']
        try:
            profile = self.registry.resolve(manifest)
            def heartbeat():
                if self.stop.is_set():
                    raise QAInterrupted('local_stop')
                response = self.client.call('GET',job)
                if not response.get('ok') or response['job']['state']!='running':
                    raise QAInterrupted('manager_fence_lost')
            report, receipt = self.engine.execute(profile, heartbeat)
            heartbeat()
            report['execution_receipt'] = receipt
            report['job_binding'] = dict(project=manifest['project'], task=claim['job']['task'],
                approval=claim['job']['approval'],adapter_release=manifest['adapter_release'],
                source_revision=manifest['source_revision'])
            report.pop('report_digest',None)
            report['report_digest'] = digest(report)
        except Exception:
            # Fixed reason only; adapter/child exception text may contain secrets.
            return self._uncertain(job,'execution_interrupted')
        response = self.client.call('POST',job,'result',dict(report=report))
        if response.get('ok'):
            return dict(state=response['job']['state'],decision=response['job']['decision'],
                        retry_performed=False,operational_authorization=False)
        # Reconcile committed-but-unacknowledged writes by reading, never resending.
        observed = self.client.call('GET',job)
        if (observed.get('ok') and observed['job']['state'] in ('accepted','blocked') and
                observed['job'].get('report',{}).get('report_digest')==report['report_digest']):
            return dict(state=observed['job']['state'],result_recovered=True,
                        retry_performed=False,operational_authorization=False)
        return self._uncertain(job,'result_delivery_uncertain')
