#!/usr/bin/env python3
"""Local host checks and actual separate-process restart challenge, no models."""
import argparse
import json
import subprocess
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.host_preflight import local_host_identity,inspect_preflight,prepare_restart_challenge,verify_restart_challenge


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=('identity','inspect','prepare','verify','restart-proof'))
    parser.add_argument('--state-root')
    parser.add_argument('--expected-host-identity')
    parser.add_argument('--expected-service-id')
    args=parser.parse_args()
    if args.phase=='identity':
        result={'observed_local_host_identity':local_host_identity(),'scope':'local_measurement_only','expected_host_verified':False,'remote_attestation_verified':False}
    else:
        if args.state_root is None or args.expected_host_identity is None:parser.error('explicit --state-root and --expected-host-identity required')
        values={'state_root':args.state_root,'expected_host_identity':args.expected_host_identity}
        if args.phase=='inspect':result=inspect_preflight(**values,expected_service_id=args.expected_service_id)
        elif args.phase=='prepare':result=prepare_restart_challenge(**values)
        elif args.phase=='verify':result=verify_restart_challenge(**values)
        else:
            # No executable or PID comes from the command line. Both children run
            # this exact trusted script, with fixed phases and bounded output/time.
            outputs=[]
            for phase in ('prepare','verify'):
                child=subprocess.run([sys.executable,'-B',str(Path(__file__).resolve()),phase,'--state-root',args.state_root,'--expected-host-identity',args.expected_host_identity],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=15)
                if child.returncode or len(child.stdout)>16384:raise RuntimeError('restart challenge child failed')
                outputs.append(json.loads(child.stdout))
            if outputs[0]['challenge_digest']!=outputs[1]['challenge_digest'] or outputs[1]['separate_process_read_verified'] is not True:raise RuntimeError('exact restart challenge mismatch')
            result={'scope':'local_actual_process_restart','process_restart_persistence_verified':True,'writer_exit_code':0,'reader_exit_code':0,'challenge_digest':outputs[0]['challenge_digest'],'infrastructure_persistence_verified':False,'reboot_verified':False,'remote_execution_performed':False,'models_started':False}
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':main()
