"""CI uses synthetic fixtures only; no credentials, private source files or outbound sockets."""
import argparse,ipaddress,json,runpy,socket,sys,unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]

def denied(*args,**kwargs):raise RuntimeError('External network is disabled during order regression checks')

def run_script(path):
    try:runpy.run_path(str(path),run_name='__main__')
    except SystemExit as error:
        if error.code not in (None,0):raise RuntimeError('Synthetic regression script failed: '+Path(path).name) from error

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    def local(address):
        try:return address[0]=='localhost' or ipaddress.ip_address(address[0]).is_loopback
        except (ValueError,TypeError,IndexError):return False
    original_create=socket.create_connection
    def create(address,*args,**kwargs):
        if local(address):return original_create(address,*args,**kwargs)
        return denied()
    socket.create_connection=create
    original=socket.socket.connect
    def connect(sock,address):
        if sock.family==socket.AF_UNIX or local(address):return original(sock,address)
        return denied()
    socket.socket.connect=connect
    result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover(str(ROOT/'tests')))
    (output/'unit-tests.json').write_text(json.dumps({'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'outbound_python_sockets_disabled':True})+'\n')
    if not result.wasSuccessful():return 1
    for name in ('edge_cases','context_checks','lifecycle_checks','assistance_checks','review_rules_checks','full_flow_checks'):
        sys.argv=[name,str(output/(name+'.json'))]
        run_script(ROOT/'validation'/(name+'.py'))
    (output/'completed.json').write_text(json.dumps({'synthetic_scripts_completed':6,'unit_tests_passed':True,'no_private_source_files':True})+'\n')
    return 0
if __name__=='__main__':raise SystemExit(main())
