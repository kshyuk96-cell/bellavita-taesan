"""Run a four-project synthetic loopback rehearsal; never contact live providers."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.control import request
from manager_bridge.store import Store
from manager_bridge.maintenance_registry import Registry
from manager_bridge.project_adapters import ProjectProvider, ProviderResult, SOURCE_COMMITS
from manager_bridge.source_watch import SourceHead, HeadProbe
from manager_bridge.source_rehearsal import observe_checkout
from manager_bridge.project_runtime import BubblewrapTests
from manager_bridge.server import make_server


def rehearse(sources):
    root=Path(__file__).resolve().parents[1]
    configs=json.loads((root/'manager_bridge/maintenance_projects.json').read_text())
    pins={p:observe_checkout(Path(sources)/p) for p in configs}
    if any(pins[p]['sha']!=SOURCE_COMMITS[p] or not pins[p]['tracked_and_visible_untracked_clean'] for p in configs):
        raise ValueError('exact reviewed source snapshots required')
    with tempfile.TemporaryDirectory(prefix='taesan-runtime-rehearsal-') as folder:
        store=Store(Path(folder)/'state.sqlite3');registry=Registry(store)
        owner=store.add_token('fixture-owner','owner',['*'])
        for project,config in configs.items():registry.register(owner,project,config)
        manager=store.add_token('fixture-manager','manager',list(configs))
        erp_only=store.add_token('fixture-erp-only','manager',['bellavita-erp'])
        now=time.time
        payloads={
            ('bellavita-erp','orders.list'):{'orders':[{'id':'13370','date':'2026-11-15','status':'접수','revision':1,'sourceRow':13370,'stableUid':'fixture-only-order'}]},
            ('bellavita-shop','orders.list'):{'orders':[{'id':'fixture-order','state':'paid','details':{'amount':93500,'title':'가상 행사'}}]},
            ('hope-sunrise','products.list'):{'products':[{'id':'fixture-product','title':'가상 여행','status':'published','price':10000}]},
            ('hope-sunrise','inquiries.list'):{'requests':[{'id':'fixture-inquiry','productId':'fixture-product','status':'접수'}]},
            ('funeral-association','access.status'):{'user':{'id':'fixture-staff','role':'staff','home_id':'fixture-home'},'active_session':True},
            ('funeral-association','obituary.get'):{'user':{'id':'fixture-staff','role':'staff','home_id':'fixture-home'},'active_session':True,
                'authorization_verified':True,'authorized_id':'fixture-obituary',
                'obituary':{'id':'fixture-obituary','status':'active','home':{'id':'fixture-home'},'version':1}},
        }
        event={'order_id':'fixture-order','event_key':'fixture-paid','revision':1,'state':'paid','source_verified':True}
        def provider(project):
            config=configs[project]
            def read(value):
                payload=event.copy() if value['action']=='order.event' else payloads[(project,value['action'])]
                return ProviderResult('ok','fixture/provider',now(),payload,SOURCE_COMMITS[project])
            return ProjectProvider(project,config['company'],config['source_ref'],config['branch'],SOURCE_COMMITS[project],read,
                bound_subject='fixture-staff' if project=='funeral-association' else None)
        server=make_server(store,port=0)
        server.project_business_providers={p:provider(p) for p in configs}
        heads=dict(SOURCE_COMMITS)
        def probe(project):
            config=configs[project]
            return HeadProbe(config['source_kind'],config['source_ref'],config['branch'],lambda p:SourceHead(p,config['source_kind'],config['source_ref'],config['branch'],heads[p],'fixture-revision','fixture/source-head',now()))
        server.project_source_probes={p:probe(p) for p in configs}
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_address[1]);prefix='/v1/representative/runtime/'
        def call(route,body,token=manager):
            result=request(base,token,'POST',prefix+route,body)
            if result.get('ok') is not True:raise RuntimeError('synthetic runtime route failed')
            return result['result']
        try:
            reads={}
            for (project,action),_ in payloads.items():
                query={'id':'fixture-obituary'} if action=='obituary.get' else {}
                result=call('business/read',{'project':project,'action':action,'query':query})
                if result['state']!='ok' or result['count']!=1:raise RuntimeError('fixture read mismatch')
                reads[project+'/'+action]={'state':result['state'],'count':result['count']}
            denied=request(base,erp_only,'POST',prefix+'business/read',{'project':'hope-sunrise','action':'products.list'})
            if denied.get('status')!=404:raise RuntimeError('fixture scope regression')
            event_states=[]
            for revision,state,key in ((1,'paid','fixture-paid'),(1,'paid','fixture-paid'),(2,'cancelled','fixture-cancel'),(3,'paid','fixture-late-paid')):
                event.update(revision=revision,state=state,event_key=key)
                event_states.append(call('business/event',{'project':'bellavita-shop','order_id':'fixture-order','event_key':key})['state'])
            if event_states!=['recorded','duplicate','recorded','needs_reconciliation']:raise RuntimeError('fixture payment lifecycle mismatch')
            for project in configs:
                if call('source/inspect',{'project':project})['state']!='baseline':raise RuntimeError('fixture source baseline mismatch')
            heads['hope-sunrise']='a'*40
            changed=call('source/inspect',{'project':'hope-sunrise'})
            pending=call('source/pending',{'projects':list(configs)})
            if changed['state']!='changed' or len(pending['events'])!=1:raise RuntimeError('fixture source detection mismatch')
            call('source/ack',{'event_id':changed['event_id']})
            if call('source/pending',{'projects':list(configs)})['events']:raise RuntimeError('fixture source acknowledgment mismatch')
            reports=[call('failures/evaluate',{'project':p}) for p in configs]
            if any(r['operational']['score_verified'] or r['model_lab']['status']!='not_measured' for r in reports):raise RuntimeError('unmeasured score regression')
            sandbox=BubblewrapTests().probe()
            return {'scope':'synthetic_loopback_only','source_snapshots_verified':len(pins),
                'source_commits':{p:v['sha'] for p,v in pins.items()},'business_reads':reads,
                'cross_project_access_denied':True,'event_states':event_states,
                'source_change_detected_and_acknowledged':True,'missing_model_not_measured':True,
                'sandbox':sandbox,'customer_providers_connected':False,
                'existing_codex_windows_connected':False,'operational_change':False,
                'payments_executed':False,'messages_sent':False,'schedules_created':False}
        finally:server.shutdown();thread.join(timeout=3);server.server_close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--sources',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    try:
        result=rehearse(args.sources)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'ok':True,'source_snapshots_verified':result['source_snapshots_verified'],
            'synthetic_read_paths':len(result['business_reads']),'sandbox':result['sandbox'],'operational_change':False}))
        return 0
    except Exception:
        print(json.dumps({'ok':False,'error':'synthetic_runtime_rehearsal_failed','operational_change':False}));return 1


if __name__=='__main__':raise SystemExit(main())
