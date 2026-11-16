// Controlled test entry point. The imported service is pinned and network-isolated.
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
import {execFileSync} from 'node:child_process';
import {LocalD1} from './d1-local.mjs';
import {createFixtureBridge,DEFINITIONS,PRODUCTS} from './fixture-bridge.mjs';
const emit=console.log.bind(console);console.log=()=>{};
const config=JSON.parse(fs.readFileSync('/input/config.json','utf8'));
const RealDate=Date;
globalThis.Date=class extends RealDate{constructor(...args){super(...(args.length?args:[config.virtual_clock]))}static now(){return RealDate.parse(config.virtual_clock)}};
const api=await import('./sut.bundle.mjs');
const results=[],transcript=[],artifacts={},db=new LocalD1();
const setupKey=[...crypto.getRandomValues(new Uint8Array(32))].map(x=>x.toString(16).padStart(2,'0')).join('');
const env={DB:db,REVIEW_DATA_KEY:btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(32))))};
const root='/tmp/site';fs.cpSync('/input/sut',root,{recursive:true});process.chdir(root);
let built;
try{await import(pathToFileURL(root+'/build-worker.mjs'));built=(await import(pathToFileURL(root+'/dist/server/index.js'))).default}
catch{emit(JSON.stringify({fatal:'original_service_build_failed',source_revision:config.source_revision,operational_authorization:false}));process.exit(2)}
const appFetch=request=>built.fetch(request,env);
const bridge=createFixtureBridge({api,db,appFetch,env,sourceRevision:config.source_revision,enabled:true,setupKey});
const assert=(ok,note='assertion failed')=>{if(!ok)throw Error(note)};
const normalized=v=>JSON.stringify(v);
function item(id,title,expected,fn){return async()=>{
 try{const actual=await fn();results.push({id,title,expected,actual,status:normalized(actual)===normalized(expected)?'PASS':'FAIL'})}
 catch(e){results.push({id,title,expected,actual:null,status:'BLOCKED',reason:e.status?'service_status_'+e.status:'execution_observation_unavailable'})}
}}
class Client{
 constructor(cookie='',runId=null,generation=null){this.cookie=cookie;this.runId=runId;this.generation=generation}
 async call(path,body=null,options={}){
  const headers={'content-type':'application/json'};if(this.cookie)headers.cookie=this.cookie;
  if(options.setup!==false&&path.startsWith('/review/qa-fixtures/'))headers['x-hope-qa-key']=setupKey;
  if(this.generation!==null)headers['x-hope-qa-generation']=String(this.generation);
  const response=await bridge.handle(new Request('https://local-qa.invalid'+path,{method:body===null?'GET':'POST',headers,body:body===null?undefined:JSON.stringify(body)}));
  const cookie=response.headers.get('set-cookie');if(cookie?.startsWith('hope_review='))this.cookie=cookie.split(';')[0];
  const raw=await response.text();let data;try{data=JSON.parse(raw)}catch{data=raw}
  transcript.push({runId:this.runId,path,status:response.status,requestId:body?.requestId||null});
  return {status:response.status,data};
 }
 async login(role='member'){
  const r=await this.call('/review/api/login',{username:role+'@hope.test',password:api.rvPassword||'HopeTest!2026'});assert(r.status===200,'login');return this;
 }
 async command(action,fields={},requestId=crypto.randomUUID()){
  const r=await this.call('/review/api/command',{action,...fields,requestId});if(r.status!==200)throw Object.assign(Error('service refused'),{status:r.status});return r.data;
 }
 async state(){const r=await this.call('/review/api/state');assert(r.status===200,'state');return r.data}
}
async function fresh(dataset='U04'){
 const c=new Client();await c.login();c.runId=crypto.randomUUID();
 const registered=await c.call('/review/qa-fixtures/v1/register',{runId:c.runId,sourceRevision:config.source_revision});assert(registered.status===200,'register');c.generation=0;
 const seeded=await c.call('/review/qa-fixtures/v1/seed',{runId:c.runId,generation:c.generation,dataset,sourceRevision:config.source_revision});
 if(seeded.status!==200)throw Object.assign(Error('seed'),{status:seeded.status});c.generation=seeded.data.generation;return c;
}
async function document(c){const session=await api.rvSession(new Request('https://local-qa.invalid/review/api/state',{headers:{cookie:c.cookie}}),db);return (await api.rvRead(db,session.workspace_id)).document}
async function snapshot(c){const doc=await document(c);const view=api.rvView(doc,api.rvAccounts[0]);return {balances:view.balances,principal:view.principal,contracts:doc.state.contracts.map(x=>({id:x.id,status:x.status,billingEnabled:x.billingEnabled,recurringEnabled:x.recurringEnabled})),
 invoices:doc.state.invoices.map(x=>({id:x.id,kind:x.kind,subtype:x.subtype,amount:x.amount,status:x.status,bookingId:x.bookingId})),
 ledger:doc.state.ledger.map(x=>({id:x.id,sourceKey:x.sourceKey,entries:x.entries,principalReturnId:x.principalReturnId})),
 bookings:doc.state.bookings,refunds:doc.state.refunds,principalRecords:api.clubPrincipalRecords(doc.state),withdrawn:view.withdrawn}}
async function purchase(c){let v=await c.state();const reply=await c.command('book',{expectedTotal:v.quote.total});
 for(const inv of (await c.state()).state.invoices.filter(i=>i.bookingId===reply.result.id&&i.status==='unpaid'))await c.command('payInvoice',{invoiceId:inv.id});return reply.result.id}
let flow,bookingId,beforeCancel;
const jobs=[
 item(21,'실제 소스 고정',{source:config.source_revision,domainFunctions:true},async()=>({source:config.source_revision,domainFunctions:typeof api.rvApply==='function'&&typeof api.coApply==='function'})),
 item(22,'실제 빌드·격리 프로필',{built:true,uid:65534,networkDenied:true,hostFsDenied:true,spawnDenied:true},async()=>{
  let networkDenied=false,hostFsDenied=false,spawnDenied=false;
  try{await fetch('https://example.com',{signal:AbortSignal.timeout(500)})}catch{networkDenied=true}
  try{fs.readFileSync('/etc/passwd')}catch{hostFsDenied=true}
  try{execFileSync('/bin/true')}catch{spawnDenied=true}
  return {built:fs.statSync(root+'/dist/server/index.js').size>100000,uid:process.getuid(),networkDenied,hostFsDenied,spawnDenied}}),
 item(23,'실제 서비스 함수 연결',{sqlite:true,active:true},async()=>{const c=await fresh('U04');return {sqlite:db.sql.prepare('SELECT count(*) AS n FROM review_workspaces').get().n>0,active:(await c.state()).state.contracts[0].status==='active'}}),
 item(24,'seed 전용 인증',{forbidden:403,unchanged:true},async()=>{const c=await fresh();const before=(await c.state()).balances;
  const r=await c.call('/review/qa-fixtures/v1/seed',{runId:c.runId,generation:c.generation,dataset:'U09',sourceRevision:config.source_revision},{setup:false});return {forbidden:r.status,unchanged:normalized(before)===normalized((await c.state()).balances)}}),
 item(25,'테스트 테이블 경계',{productionDenied:true,attachDenied:true},async()=>{let a=false,b=false;try{db.prepare('SELECT * FROM club_state')}catch{a=true}try{db.prepare("ATTACH DATABASE '/tmp/business.sqlite' AS business")}catch{b=true}return {productionDenied:a,attachDenied:b}}),
 item(26,'U01~U10 실제 회원 데이터',{count:10,matched:true},async()=>{let count=0;const failures=[];
  for(const id of Object.keys(DEFINITIONS)){try{const c=await fresh(id),v=await c.state();if(DEFINITIONS[id].months)for(const book of ['DP','BP','SP'])assert(v.balances[book]===DEFINITIONS[id][book],id+book);count++}catch(e){failures.push({id,status:e.status||null})}}
  artifacts.preparationFailures=failures;return {count,matched:failures.length===0}}),
 item(27,'실제 원금·포인트 원장',{DP:600000,BP:150000,SP:100000,sourceIntegrity:true,paidInvoices:12},async()=>{const c=await fresh(),v=await c.state(),doc=await document(c);return {DP:v.balances.DP,BP:v.balances.BP,SP:v.balances.SP,sourceIntegrity:v.principal.sourceIntegrity,paidInvoices:doc.state.invoices.filter(i=>i.status==='paid'&&i.kind!=='travel').length}}),
 item(28,'실제 상품/객실/재고 견적',{count:4,lastSeat:1,soldOutDenied:true,roomTypes:2},async()=>{const c=await fresh(),doc=await document(c);let denied=false;
  try{bridge.select(doc,PRODUCTS[2])}catch{denied=true}const p02=bridge.select(doc,PRODUCTS[1]);bridge.select(doc,PRODUCTS[3]);return {count:PRODUCTS.length,lastSeat:p02.offer.stock,soldOutDenied:denied,roomTypes:doc.meta.selectedCatalog.config.offers.length}}),
 item(29,'원거래 기반 납부 출처',{issued:1200000,remaining:750000,manualDP:false,balanced:true},async()=>{const c=await fresh('U09'),doc=await document(c),ct=doc.state.contracts[0];return {
  issued:doc.state.invoices.filter(i=>i.status==='paid'&&i.kind!=='travel').reduce((n,i)=>n+i.lines.filter(l=>l.kind==='monthly').reduce((a,l)=>a+l.amount,0),0),
  remaining:api.clubPrincipalState(doc.state,ct.id).availablePrincipal,manualDP:doc.state.ledger.some(j=>j.sourceKey.startsWith('manual:')&&j.entries.some(e=>e.book==='DP')),
  balanced:doc.state.ledger.every(j=>['DP','BP','SP','CP'].every(book=>j.entries.filter(e=>e.book===book).reduce((n,e)=>n+e.delta,0)===0))}}),
 item(30,'세대 초기화·롤백·증거',{staleDenied:409,generation:2,archived:true,rollback:true},async()=>{const c=await fresh();const r=await c.call('/review/qa-fixtures/v1/seed',{runId:c.runId,generation:1,dataset:'U04',sourceRevision:config.source_revision});assert(r.status===200);const stale=await c.call('/review/api/command',{action:'registerCard',scenario:'success',requestId:crypto.randomUUID()});c.generation=2;
  const prior=db.sql.prepare('SELECT count(*) AS n FROM qa_fixture_events').get().n;let rollback=false;
  try{db.batch([db.prepare('INSERT INTO qa_fixture_events(run_id,generation,kind,receipt) VALUES(?,?,?,?)').bind(c.runId,2,'rollback-test','{}'),db.prepare('INSERT INTO qa_fixture_cas(ok) SELECT 0')])}catch{rollback=db.sql.prepare('SELECT count(*) AS n FROM qa_fixture_events').get().n===prior}
  return {staleDenied:stale.status,generation:r.data.generation,archived:db.sql.prepare('SELECT count(*) AS n FROM qa_fixture_snapshots WHERE run_id=?').get(c.runId).n===2,rollback}}),
 item(31,'사전 기대값과 실제 준비 대조',{matched:true,tamperDetected:true},async()=>{const c=await fresh();const doc=await document(c),view=await c.state();const before=view.balances.DP===600000&&view.balances.BP===150000&&view.balances.SP===100000;
  doc.state.ledger.find(j=>j.entries.some(e=>e.account===doc.state.contracts[0].id&&e.book==='DP')).entries.find(e=>e.account===doc.state.contracts[0].id&&e.book==='DP').delta++;
  const p=api.clubPrincipalState(doc.state,doc.state.contracts[0].id);return {matched:before,tamperDetected:p.sourceIntegrity===false}}),
 item(32,'실제 로그인·역할·실행 공간',{memberDenied:403,financeDenied:403,anonymous:401,otherIsolated:true},async()=>{const c=await fresh();const denied=await c.call('/review/api/command',{action:'runCycle',requestId:crypto.randomUUID()});await c.login('finance');const finance=await c.call('/review/api/command',{action:'approvePrincipal',settlementId:crypto.randomUUID(),requestId:crypto.randomUUID()});const anonymous=await new Client().call('/review/api/state');const other=await fresh('U05');return {memberDenied:denied.status,financeDenied:finance.status,anonymous:anonymous.status,otherIsolated:(await other.state()).balances.DP===0}}),
 item(33,'실제 3등급 가입',{hope:75000,happy:150000,premium:300000},async()=>{const out={};artifacts.signup={};for(const [id,grade] of [['U01','hope'],['U02','happy'],['U03','premium']]){const c=await fresh(id);await c.command('registerCard',{scenario:'success'});await c.command('signup',{grade,consents:{terms:true,privacy:true,recurring:true,marketing:false}});const state=await c.state();out[grade]=state.state.invoices.find(i=>i.kind==='initial').amount;artifacts.signup[grade]={paid:out[grade],DP:state.balances.DP,BP:state.balances.BP,status:state.state.contracts[0].status}}return out}),
 item(34,'실제 U04/P02 혼합결제',{DP:600000,BP:100000,SP:100000,card:200000,confirmed:true,nonzeroCardApprovals:1},async()=>{flow=await fresh();const v=await flow.state(),plan=v.paymentPlan;bookingId=await purchase(flow);const after=await flow.state(),doc=await document(flow);beforeCancel=await snapshot(flow);return {DP:plan.DP,BP:plan.BP,SP:plan.SP,card:plan.card,confirmed:after.state.bookings.find(b=>b.id===bookingId).status==='confirmed',nonzeroCardApprovals:doc.state.travelEvents.filter(e=>e.bookingId===bookingId&&e.type==='succeeded'&&e.amount>0).length}}),
 item(35,'실제 R01 취소·복원',{DP:600000,BP:150000,SP:100000,cancelled:true,cardReturned:200000,lotOrigins:true,noIssues:true},async()=>{assert(flow&&bookingId);const r=await flow.command('requestRefund',{bookingId,rooms:1});await flow.login('finance');await flow.command('reviewRefund',{refundId:r.result.id});await flow.login('admin');await flow.command('approveRefund',{refundId:r.result.id});await flow.command('executeRefund',{refundId:r.result.id});await flow.login('member');const v=await flow.state(),doc=await document(flow);artifacts.cancellation={before:beforeCancel,after:await snapshot(flow)};return {DP:v.balances.DP,BP:v.balances.BP,SP:v.balances.SP,cancelled:v.state.bookings.find(b=>b.id===bookingId).status==='cancelled',cardReturned:doc.state.travelEvents.filter(e=>e.type==='refunded').reduce((n,e)=>n+e.amount,0),lotOrigins:api.ciLots(doc.state).some(l=>l.originalLotId),noIssues:api.clubReconcile(doc.state).length===0}}),
 item(36,'실제 U09 탈퇴·75만원 상한',{cap:750000,returned:750000,bonusCash:0,stopped:true,withdrawn:true},async()=>{const c=await fresh('U09');await c.command('stopMembership');const before=await c.state();const r=await c.command('requestPrincipal',{amount:750000});await c.login('finance');await c.command('reviewPrincipal',{settlementId:r.result.id});await c.login('admin');await c.command('approvePrincipal',{settlementId:r.result.id});await c.login('finance');await c.command('executePrincipal',{settlementId:r.result.id});await c.login('member');await c.command('withdrawMembership',{confirm:true});const doc=await document(c);artifacts.withdrawal=await snapshot(c);return {cap:before.principal.returnCap,returned:api.clubPrincipalRecords(doc.state,'executed').reduce((n,x)=>n+x.amount,0),bonusCash:doc.state.ledger.filter(j=>j.principalReturnId).flatMap(j=>j.entries).filter(e=>e.account===doc.state.contracts[0].id&&e.book!=='DP').reduce((n,e)=>n+e.delta,0),stopped:doc.state.contracts[0].billingEnabled===false&&doc.state.contracts[0].recurringEnabled===false,withdrawn:doc.state.privacyRequests.some(x=>x.kind==='withdraw'&&x.status==='processed')}}),
 item(37,'실제 HTML/API/원장 대조',{html:true,api:true,journal:true,requestIds:true},async()=>{const c=await fresh(),v=await c.state(),page=await c.call('/review/app'),doc=await document(c),ct=doc.state.contracts[0];artifacts.server_html=page.data;return {html:typeof page.data==='string'&&page.data.includes('600,000')&&page.data.includes('data-testid="balance-DP"'),api:v.balances.DP===600000,journal:doc.state.ledger.flatMap(j=>j.entries).filter(e=>e.account===ct.id&&e.book==='DP').reduce((n,e)=>n+e.delta,0)===600000,requestIds:transcript.some(e=>e.requestId!==null)}}),
 item(39,'기존 결함 수정 버전 재시험',{runtimeDPFirst:true,scopeSeparated:true,termsExpanded:true},async()=>{const c=await fresh();const doc=await document(c);bridge.select(doc,{id:'qa-400k',title:'TEST DP 충분',target:400000,stock:1,category:'overseas'});const plan=api.rvView(doc,api.rvAccounts[0]).paymentPlan;
  const policy=await c.call('/review/policy'),caps=await c.call('/review/status.json');const termNames=['본인 납입금 잔액','추가 보너스포인트','특별포인트','법인 지원포인트'];artifacts.terms_pages=[];for(const path of ['/review/','/review/features','/review/app','/review/policy']){const p=await c.call(path);artifacts.terms_pages.push({path,html:p.data})}
  artifacts.defectRetest={sourceRevision:config.source_revision,policyHTML:policy.data,terms:termNames.map(n=>({name:n,present:policy.data.includes(n)}))};return {runtimeDPFirst:plan.DP===400000&&plan.BP===0&&plan.SP===0&&plan.card===0,scopeSeparated:caps.data.coverageScope==='previous_v1_not_current_uat_compliance',termsExpanded:termNames.every(n=>policy.data.includes(n))}}),
 item(40,'고객 38개 증거 판정 연동',{realPG:false,actualBrowser:false,customerApproved:false},async()=>({realPG:api.rvCapabilities(env).actualPgCardTests,actualBrowser:false,customerApproved:false}))
];
for(const job of jobs)await job();
results.push({id:38,title:'데스크톱·390px 실제 브라우저 캡처',status:'BLOCKED',expected:'supported cua_repl preview route',actual:null,reason:'cua_repl_not_available_no_browser_fallback'});
results.sort((a,b)=>a.id-b.id);
emit(JSON.stringify({schema:'hope-actual-service-batch02/v1',source_revision:config.source_revision,scope:'actual_service_source_local_sqlite_simulator',
 virtual_clock:config.virtual_clock,items:results,counts:Object.fromEntries(['PASS','FAIL','BLOCKED'].map(s=>[s,results.filter(r=>r.status===s).length])),
 transcript,artifacts,sqlStatements:db.statements,realPG:false,operational_authorization:false,customer_acceptance_approved:false}));db.close();
