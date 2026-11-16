"""Run reviewed offline component QA, with network/process/write audit restrictions."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from taesan_core.qa_order_ledger import adapter, suite
from taesan_core.universal_qa import run_scenarios, build_contract
from manager_bridge.qa_admission import admit_qa_report


def main():
    revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    paths = ['taesan_core/qa_order_ledger.py','taesan_core/universal_qa.py',
             'manager_bridge/qa_admission.py','manager_bridge/store.py',
             'order_ledger.py','order_intake.py','invitation.py','checkout.py','blogpay.py',
             'scripts/run_order_ledger_qa.py']
    for path in paths:
        if subprocess.check_output(['git','show',revision+':'+path],cwd=ROOT) != (ROOT/path).read_bytes():
            raise ValueError('commit executable source before reporting: '+path)
    # Imports are reviewed repository code. This audit policy is defense in depth,
    # not an OS isolation boundary for arbitrary or hostile adapters.
    import order_ledger
    def audit(event, values):
        if event.startswith(('socket.', 'subprocess.', 'os.system', 'os.exec', 'os.spawn')):
            raise PermissionError('offline component execution only')
        if event == 'open' and len(values) >= 3 and values[2] & (1 | 2 | 64 | 512 | 1024):
            raise PermissionError('read-only component execution')
    registrations = [adapter()]
    scenarios = suite()
    contract = build_contract(scenarios, registrations)
    # Hook runs only in this short-lived child process. Export is delegated to
    # stdout so no write permission is needed by tested code.
    sys.addaudithook(audit)
    report = run_scenarios(scenarios, registrations, source_revision=revision,
                           seed=20261116, required_coverage=['ledger-date-time-channel'])
    decision = admit_qa_report(report, expected_source_revision=revision,
        required_coverage=['ledger-date-time-channel'], expected_contract=contract)
    print(json.dumps(dict(report=report,contract=contract,decision=decision),ensure_ascii=False))
    return 0 if decision['admitted'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
