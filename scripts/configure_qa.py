"""Owner-only provisioning of host profiles and QA contracts; no task execution."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.control import secret
from manager_bridge.qa_runtime import QAProfile, QARegistry, QASandbox
from manager_bridge.qa_jobs import QAJobs
from manager_bridge.store import Store
from taesan_core.universal_qa import build_contract


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['preflight','approve'])
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--project',required=True)
    parser.add_argument('--release',required=True)
    parser.add_argument('--database',type=Path)
    parser.add_argument('--owner-token-file',type=Path)
    parser.add_argument('--task')
    args=parser.parse_args()
    config=json.loads(args.config.read_text())
    registry=QARegistry([QAProfile(**row) for row in config['profiles']])
    profile=registry.profiles[(args.project,args.release)]
    profile.source_check()
    if args.command=='preflight':
        print(json.dumps(dict(profile_digest=profile.fingerprint,profile=profile.manifest(),
                             sandbox=QASandbox().probe(),execution_started=False)))
        return
    if args.database is None or not args.database.is_file() or args.owner_token_file is None:
        parser.error('existing private manager database and owner credential required')
    # Contracts must come from reviewed source; loading a candidate's arbitrary
    # module in the control process would break the isolation boundary.
    if profile.source.resolve()!=Path(__file__).resolve().parents[1]:
        parser.error('approve contracts from the exact reviewed control checkout; use host API for external SUT')
    if profile.suite=='order-ledger-v1':
        from taesan_core.qa_order_ledger import suite,adapter
        scenarios,registrations,coverage=suite(),[adapter()],['ledger-date-time-channel']
    else:
        from taesan_core.qa_fixtures import build_suite,adapters,REQUIRED_COVERAGE
        scenarios,registrations,coverage=build_suite(profile.seed),adapters(),REQUIRED_COVERAGE
    jobs=QAJobs(Store(args.database));owner=secret(args.owner_token_file)
    approval=jobs.approve(owner,project=profile.project,source_revision=profile.revision,
        contract=build_contract(scenarios,registrations),coverage=list(coverage),
        adapter_release=profile.release,max_seconds=profile.timeout,execution_profile_digest=profile.fingerprint)
    if args.task:jobs.require_for_task(owner,args.task,approval)
    print(json.dumps(dict(approval=approval,profile_digest=profile.fingerprint,execution_started=False)))

if __name__=='__main__':
    main()
