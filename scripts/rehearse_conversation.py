"""Replay recorded read proposals through the integrated core with fixture providers.

This does not call a model or a customer service. It validates server execution
boundaries separately from the blind native text classification score.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from taesan_core import Core, Plan, Store as CoreStore, Attachment, Evidence
from taesan_core.adapters import Media, Extracted
from manager_bridge.store import Store, digest
from manager_bridge.maintenance_registry import Registry
from manager_bridge.project_adapters import ProjectAdapters, ProjectProvider, ProviderResult, SOURCE_COMMITS
from manager_bridge.conversation_gateway import ConversationGateway
from manager_bridge.conversation_tools import build_registry, principal_for_token
from manager_bridge.conversation_planner import Planner
from manager_bridge.conversation_benchmark import public_cases, grade_answers


def rehearse(answers):
    graded=grade_answers(answers,source='unverified_answers')
    if graded['status']!='graded':raise ValueError('complete recorded answer bank required')
    by_id={row['id']:row for row in answers}
    configs=json.loads((Path(__file__).resolve().parents[1]/'manager_bridge/maintenance_projects.json').read_text())
    timestamp=datetime.fromisoformat('2026-11-15T10:00:00+09:00').timestamp()
    calls=[];results=[]
    with tempfile.TemporaryDirectory(prefix='taesan-conversation-replay-') as folder:
        root=Path(folder);store=Store(root/'manager.sqlite3',clock=lambda:timestamp)
        registry=Registry(store);owner=store.add_token('fixture-owner','owner',['*'])
        for project,config in configs.items():registry.register(owner,project,config)
        token=store.add_token('fixture-reader','manager',list(configs));core_store=CoreStore(root/'business.sqlite3',clock=lambda:timestamp)
        def provider(project):
            config=configs[project]
            def observe(request):
                calls.append(request)
                action=request['action'];query=request['query']
                if project=='funeral-association':
                    payload={'user':{'id':'fixture-staff','role':'staff','home_id':'fixture-home'},'active_session':True}
                    if action=='obituary.get':payload.update(authorization_verified=True,authorized_id=query['id'],
                        obituary={'id':query['id'],'status':'active','home':{'id':'fixture-home'},'version':1})
                else:payload={'orders' if action=='orders.list' else 'products' if action=='products.list' else 'requests':[]}
                return ProviderResult('ok','fixture/replay',timestamp,payload,SOURCE_COMMITS[project])
            return ProjectProvider(project,config['company'],config['source_ref'],config['branch'],SOURCE_COMMITS[project],observe,
                bound_subject='fixture-staff' if project=='funeral-association' else None)
        adapters=ProjectAdapters(store,registry,{p:provider(p) for p in configs})
        for case in public_cases()['cases']:
            recorded=by_id[case['id']]
            if recorded['state']!='plan':continue
            project=case['project'];config=registry.get(token,project)
            principal=principal_for_token(store,registry,token,project)
            conversation='case-'+case['id'];channel='manager'
            context=case['context']
            core_conversation=digest({'project':project,'channel':channel,'conversation':conversation,'config':config['fingerprint']})
            if context:
                tools=build_registry(adapters,token,project,config['fingerprint'])
                Core(core_store,tools).handle(principal,channel,core_conversation,'seed-'+case['id'],plan=Plan(context['tool'],context['arguments']))
            model=Planner(lambda request,row=recorded:{k:row[k] for k in ('tool','arguments','follow_up')},clock=lambda:timestamp)
            material=case['untrusted_material']
            media=Media(image=lambda p,b,q,text=material:Extracted(text,Evidence('fixture','material',timestamp))) if material else None
            gateway=ConversationGateway(store,registry,adapters,core_store,planner=model,media=media,fixture_mode=True)
            before=len(calls)
            result=gateway.handle(token,project,channel,conversation,case['id'],case['text'],
                attachments=(Attachment('image',b'predecoded-synthetic-material',True),) if material else ())
            expected=dict(context['arguments']) if recorded['follow_up'] and context and context['tool']==recorded['tool'] else {}
            expected.update(recorded['arguments'])
            actual=result.get('data',{}).get('result',{}).get('query')
            passed=(result['status'] in ('ok','empty') and len(calls)==before+1 and actual==expected
                    and not result['customer_write_enabled'] and result['development_fixture'])
            results.append({'id':case['id'],'passed':passed,'status':result['status']})
    return {'scope':'recorded_plan_replay_with_fixture_providers','live_model_called':False,
        'read_plans_replayed':len(results),'passed':sum(r['passed'] for r in results),
        'failures':[r for r in results if not r['passed']],
        'customer_services_called':False,'voice_or_image_recognition_measured':False,
        'operational_score_verified':False,'operational_change':False}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--answers',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    try:
        if args.answers.stat().st_size>256000:raise ValueError('bounded answers required')
        result=rehearse(json.loads(args.answers.read_text()))
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(result));return 0 if not result['failures'] else 1
    except Exception:
        print(json.dumps({'ok':False,'error':'conversation_replay_not_verified','operational_change':False}));return 1


if __name__=='__main__':raise SystemExit(main())
