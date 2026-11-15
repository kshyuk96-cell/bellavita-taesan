"""Execute the actual browser script across auth/stale/error boundaries.

The DOM harness intentionally rejects HTML insertion and persistent storage. It
runs the delivered JavaScript rather than reconstructing its routing behavior.
HTTP checks use the real manager server's static allowlist and security headers.
"""
from html.parser import HTMLParser
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from urllib.request import urlopen

from manager_bridge.server import make_server
from manager_bridge.store import Store

UI=Path(__file__).resolve().parents[1]/'ui'


class ShellParser(HTMLParser):
    def __init__(self):super().__init__();self.tags=[]
    def handle_starttag(self,tag,attrs):self.tags.append((tag,dict(attrs)))


NODE_HARNESS=r"""
'use strict';
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const root=process.argv[2],scenario=process.argv[3];
class Element {
 constructor(tag,id=''){this.tagName=tag.toUpperCase();this.id=id;this.children=[];this._text='';this.value='';this.disabled=false;this.events={};this.attributes={};}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}
 set innerHTML(value){throw Error('unsafe HTML insertion');}
 get innerHTML(){throw Error('unsafe HTML read');}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this.children=[...nodes];this._text='';}
 setAttribute(key,value){this.attributes[key]=String(value);}
 addEventListener(kind,callback){this.events[kind]=callback;}
}
const html=fs.readFileSync(root+'/three-tier.html','utf8'),elements={};
for(const match of html.matchAll(/<([a-z]+)\b[^>]*\bid="([^"]+)"/g))elements[match[2]]=new Element(match[1],match[2]);
const document={getElementById:id=>{assert(elements[id],'missing shell element '+id);return elements[id];},createElement:tag=>new Element(tag)};
const requests=[];let mode='success',resolvePending=null;
const token='m'.repeat(44),injection='<img src=x onerror="globalThis.executed=true">';
const task={task_id:'task-one',state:'accepted',state_label:'기술 인수 완료',head_sha:'a'.repeat(40),technical_accepted:true,rework_count:0,checks:{passed:1,failed:0,unmeasured:0}};
function summary(){return {snapshot_scope:'authorized_registered_hierarchy',operational_change:false,
 projects:[{project:'a',name:injection,registered_members:2,connection_verified_members:0,capacity:{configured:true,review_backlog_observed:false},tasks:[task],tasks_truncated:false}],
 counts_displayed:{developing:0,rework_developing:0,candidate_ready:0,reviewing:0,accepted:1},owner_decisions:[],manager_attention:[],owner_decision_count:0,manager_attention_count:0};}
function response(body,status=200){return {ok:status>=200&&status<300,status,json:async()=>body};}
async function fetch(path,options){
 requests.push({path,options:{...options,headers:{...options.headers}}});
 if(path==='/v1/auth/logout')return response({ok:true});
 if(path==='/v1/auth/redeem')return response({ok:true});
 assert.strictEqual(path,'/v1/representative/runtime/three-tier/overview');
 if(mode==='pending')return new Promise(resolve=>{resolvePending=resolve;});
 if(mode==='expired')return response({ok:false},401);
 if(mode==='not_configured')return response({ok:true,result:{state:'not_configured',operational_change:false}});
 if(mode==='network')throw Error('private backend diagnostic must not be shown');
 const result=summary();
 if(mode==='contradiction'){result.projects[0].tasks=[{...task,technical_accepted:false,acceptance_needs_verification:true}];}
 return response({ok:true,result});
}
const context={document,fetch,AbortSignal,AbortController,setTimeout,clearTimeout,Set,Error,Number,Array,String,JSON,Promise};
Object.defineProperty(context,'localStorage',{get(){throw Error('persistent storage forbidden');}});
Object.defineProperty(context,'sessionStorage',{get(){throw Error('persistent storage forbidden');}});
context.console={log(){throw Error('console output forbidden');},error(){throw Error('console output forbidden');}};
vm.createContext(context);vm.runInContext(fs.readFileSync(root+'/three-tier.js','utf8'),context);
function allText(){return Object.values(elements).map(n=>n.textContent).join('\n');}
async function connect({bearer=true,code=''}={}){
 elements['tier-projects'].value='a,b';elements['tier-token'].value=bearer?token:'';elements['tier-pairing'].value=code;
 await elements['tier-connect'].events.submit({preventDefault(){}});
}
(async()=>{
 if(scenario==='memory_auth'){
  await connect();const read=requests.find(r=>r.path.endsWith('/overview'));
  assert(read);assert.strictEqual(read.options.method,'POST');assert.strictEqual(read.options.headers.Authorization,'Bearer '+token);
  assert.strictEqual(read.options.headers['X-Taesan-Client'],'dashboard');assert.strictEqual(read.options.credentials,'omit');
  assert.strictEqual(read.options.redirect,'error');assert.strictEqual(read.options.cache,'no-store');
  assert.deepStrictEqual(JSON.parse(read.options.body),{projects:['a','b']});
  assert(!read.path.includes(token));assert(!read.options.body.includes(token));
  assert.strictEqual(elements['tier-token'].value,'');assert.strictEqual(elements['tier-pairing'].value,'');
  assert(allText().includes(injection));assert(!allText().includes(token));
  assert(allText().includes('운영 배포 미확인'));assert(allText().includes('현재 전달된 기록에 형님 결정 요청이 없습니다.'));
  assert(!JSON.stringify(elements['tier-boards']).includes('"tagName":"IMG"'));
 }else if(scenario==='cookie_csrf'){
  await connect({bearer:false,code:'ABCDEFGHIJKL'});
  const redeem=requests.find(r=>r.path.endsWith('/redeem')),read=requests.find(r=>r.path.endsWith('/overview'));
  assert(redeem);assert.strictEqual(redeem.options.credentials,'same-origin');assert.strictEqual(read.options.credentials,'same-origin');
  assert(!('Authorization' in read.options.headers));assert.strictEqual(read.options.headers['X-Taesan-Client'],'dashboard');
  assert(!read.options.body.includes('ABCDEFGHIJKL'));assert(!allText().includes('ABCDEFGHIJKL'));
 }else if(scenario==='stale_after_disconnect'){
  await connect();mode='pending';const pending=elements['tier-refresh'].events.click();
  assert(resolvePending);await elements['tier-disconnect'].events.click();
  resolvePending(response({ok:true,result:summary()}));await pending;await new Promise(resolve=>setTimeout(resolve,0));
  assert.strictEqual(elements['tier-boards'].children.length,0);assert.strictEqual(elements['tier-counts'].children.length,0);
  assert(elements['tier-refresh'].disabled);assert(allText().includes('연결을 해제'));
 }else if(scenario==='expired'){
  await connect();mode='expired';await elements['tier-refresh'].events.click();await new Promise(resolve=>setTimeout(resolve,0));
  assert.strictEqual(elements['tier-boards'].children.length,0);assert.strictEqual(elements['tier-owner'].children.length,0);
  assert(elements['tier-refresh'].disabled);assert(allText().includes('만료·해제'));
 }else if(scenario==='unconfigured'){
  mode='not_configured';await connect();assert.strictEqual(elements['tier-boards'].children.length,0);
  assert(allText().includes('아직 연결되지 않았습니다'));assert(!allText().includes('현황을 확인했습니다'));
 }else if(scenario==='contradiction'){
  mode='contradiction';await connect();assert(allText().includes('기술 인수 근거 재확인 필요'));
  const acceptedCard=elements['tier-counts'].children.find(n=>n.children[0].textContent==='기술 인수');
  assert.strictEqual(acceptedCard.children[1].textContent,'0');
 }else if(scenario==='changed_scope'){
  await connect();mode='pending';const pending=elements['tier-refresh'].events.click();assert(resolvePending);
  elements['tier-projects'].value='b';elements['tier-projects'].events.input();
  resolvePending(response({ok:true,result:summary()}));await pending;await new Promise(resolve=>setTimeout(resolve,0));
  assert.strictEqual(elements['tier-boards'].children.length,0);assert(allText().includes('프로젝트 선택이 바뀌었습니다'));
 }else if(scenario==='api_key_rejected'){
  elements['tier-projects'].value='a';elements['tier-token'].value='sk-proj-'+token;
  await elements['tier-connect'].events.submit({preventDefault(){}});
  assert.strictEqual(requests.length,0);assert.strictEqual(elements['tier-token'].value,'');
  assert(!allText().includes(token));assert(allText().includes('전용 관리 토큰만'));
 }else throw Error('unknown scenario');
 process.stdout.write('OK '+scenario+'\n');
})().catch(()=>{process.stderr.write('UI behavior assertion failed\n');process.exitCode=1;});
"""


class ThreeTierUIShellTests(unittest.TestCase):
    def test_shell_is_read_only_accessible_and_has_only_external_resources(self):
        html=(UI/'three-tier.html').read_text();parser=ShellParser();parser.feed(html)
        scripts=[attrs for tag,attrs in parser.tags if tag=='script']
        self.assertEqual(scripts,[{'src':'/three-tier.js','defer':None}])
        self.assertTrue(any(tag=='html' and attrs.get('lang')=='ko' for tag,attrs in parser.tags))
        self.assertTrue(any(attrs.get('role')=='status' and attrs.get('aria-live')=='polite' for _,attrs in parser.tags))
        self.assertFalse(any(key.startswith('on') for _,attrs in parser.tags for key in attrs))
        for text in ('기술 인수는 운영 배포 승인이 아닙니다','기존 별도 Codex 채팅 전체 제어','OpenAI API 키를 입력하는 곳이 아닙니다'):
            self.assertIn(text,html)
        passwords={attrs.get('id'):attrs for tag,attrs in parser.tags if tag=='input' and attrs.get('type')=='password'}
        self.assertEqual(set(passwords),{'tier-token','tier-pairing'})

    def run_scenario(self,scenario):
        node=shutil.which('node')
        if not node:self.skipTest('Node runtime unavailable for actual script behavior check')
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'test-ui.cjs';path.write_text(NODE_HARNESS)
            result=subprocess.run([node,str(path),str(UI),scenario],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout,'OK '+scenario+'\n')

    def test_bearer_is_memory_only_and_dom_output_is_safe(self):self.run_scenario('memory_auth')
    def test_cookie_auth_uses_existing_csrf_request_boundary(self):self.run_scenario('cookie_csrf')
    def test_late_response_after_disconnect_does_not_restore_private_data(self):self.run_scenario('stale_after_disconnect')
    def test_expired_auth_clears_previous_project_data(self):self.run_scenario('expired')
    def test_missing_host_registration_is_not_claimed_connected(self):self.run_scenario('unconfigured')
    def test_contradictory_accepted_state_does_not_inflate_technical_count(self):self.run_scenario('contradiction')
    def test_late_response_after_project_change_is_discarded(self):self.run_scenario('changed_scope')
    def test_openai_key_is_not_sent_to_manager_auth(self):self.run_scenario('api_key_rejected')

    def test_real_server_serves_public_shell_with_restricted_security_headers(self):
        with tempfile.TemporaryDirectory() as temporary:
            store=Store(Path(temporary)/'manager.sqlite3');server=make_server(store,port=0)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                base='http://127.0.0.1:'+str(server.server_address[1])
                for route,mime,filename in (('/three-tier','text/html','three-tier.html'),('/three-tier.js','application/javascript','three-tier.js'),('/three-tier.css','text/css','three-tier.css')):
                    with urlopen(base+route) as response:
                        self.assertEqual(response.read(),(UI/filename).read_bytes())
                        self.assertIn(mime,response.headers['Content-Type'])
                        self.assertEqual(response.headers['Cache-Control'],'no-store')
                        self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])
                        self.assertIn("connect-src 'self'",response.headers['Content-Security-Policy'])
            finally:server.shutdown();thread.join(timeout=3);server.server_close()
