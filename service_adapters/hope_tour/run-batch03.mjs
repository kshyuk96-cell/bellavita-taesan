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
const checks={networkDenied:false,hostFsDenied:false,spawnDenied:false,uid:process.getuid()};
try{await fetch('https://example.com',{signal:AbortSignal.timeout(500)})}catch{checks.networkDenied=true}
try{fs.readFileSync('/etc/passwd')}catch{checks.hostFsDenied=true}
try{execFileSync('/bin/true')}catch{checks.spawnDenied=true}
artifacts.confinement={...checks,verified:checks.uid===65534&&checks.networkDenied&&checks.hostFsDenied&&checks.spawnDenied};
const pages=[];
for(const path of ['/review/','/review/features','/review/app','/review/policy']){
 const c=await fresh();pages.push({path,html:(await c.call(path)).data});
}
artifacts.terms_pages=pages;
const names=['본인 납입금 잔액(DP)','추가 보너스포인트(BP)','특별포인트(SP)','법인 지원포인트(CP)'];
const adminNames=['전자결제대행사(PG)','시스템 연동 기능(API)','고객관계관리(CRM)','품질검증(QA)','핵심성과지표(KPI)'];
const jobs=[
 item(41,'원금/포인트 전체명칭',{matched:true},async()=>({matched:pages.every(p=>names.every(n=>p.html.includes(n)))})),
 item(42,'공통 업무용어 사전',{matched:true},async()=>({matched:pages.every(p=>adminNames.every(n=>p.html.includes(n)))})),
 item(43,'네이티브 용어 도움말 구조',{nativeDetails:9,summary:9,focusCSS:true},async()=>({nativeDetails:(pages[0].html.match(/<details class="review-term">/g)||[]).length,summary:(pages[0].html.match(/<summary>/g)||[]).length,focusCSS:fs.readFileSync(root+'/dist/member-accessibility.css','utf8').includes('.review-term summary:focus-visible')})),
 item(55,'원금 0/충분/1원 경계',{U05:{DP:0,BP:0,SP:0,card:1000000},U06:{DP:1000000,BP:0,SP:0,card:0},U07:{DP:999999,BP:1,SP:0,card:0}},async()=>{
  const out={};artifacts.boundaries={};for(const id of ['U05','U06','U07']){const c=await fresh(id),v=await c.state();const {DP,BP,SP,card}=v.paymentPlan;out[id]={DP,BP,SP,card};await purchase(c);artifacts.boundaries[id]=await snapshot(c)}return out}),
 item(56,'3등급×3상품 BP 한도',{count:9,matched:true},async()=>{
  const rows=[];for(const [dataset,grade] of [['U01','hope'],['U02','happy'],['U03','premium']])for(const product of PRODUCTS.slice(0,3)){
   const c=await fresh(dataset);await c.command('registerCard',{scenario:'success'});await c.command('signup',{grade,consents:{terms:true,privacy:true,recurring:true,marketing:false}});
   const doc=await document(c),ct=doc.state.contracts[0];
   doc.state=api.coApply(doc.state,{action:'grant',contractId:ct.id,book:'BP',amount:2000000,sourceKey:'TEST-bp-limit',reason:'TEST only',day:doc.meta.day,acknowledgeSandbox:true},{id:api.rvAccounts[1].actorId,role:api.rvAccounts[1].role},doc.meta.day+'T01:00:00Z').state;
   bridge.select(doc,{...product,stock:1});const view=api.rvView(doc,api.rvAccounts[0]);rows.push({grade,category:product.category,total:view.quote.total,DP:view.paymentPlan.DP,BP:view.paymentPlan.BP,SP:view.paymentPlan.SP,card:view.paymentPlan.card});
  }artifacts.bpMatrix=rows;return {count:rows.length,matched:rows.every(r=>r.BP===Math.min(r.total-r.DP,Math.floor(r.total*({hope:{domestic:1500,overseas:1000,culture:5000},happy:{domestic:2000,overseas:1500,culture:7000},premium:{domestic:2500,overseas:1800,culture:10000}}[r.grade][r.category])/10000)))}}),
 item(57,'카드거절·동일 요청 중복',{declinedNoLoss:true,duplicatesSingleBooking:true,duplicateConflict:409},async()=>{
  const c=await fresh();await c.command('registerCard',{scenario:'decline'});const before=await c.state(),book=await c.command('book',{expectedTotal:before.quote.total});
  const inv=(await c.state()).state.invoices.find(i=>i.bookingId===book.result.id&&i.kind==='travel'&&i.subtype==='deposit');await c.command('payInvoice',{invoiceId:inv.id});
  const after=await c.state();const d=await fresh(),v=await d.state(),requestId=crypto.randomUUID();
  const [a,b]=await Promise.all([d.command('book',{expectedTotal:v.quote.total},requestId),d.command('book',{expectedTotal:v.quote.total},requestId)]);
  const clash=await d.call('/review/api/command',{action:'book',expectedTotal:v.quote.total+1,requestId});
  artifacts.declined={before:before.balances,after:after.balances,bookingStatus:after.state.bookings.find(x=>x.id===book.result.id).status};
  return {declinedNoLoss:normalized(before.balances)===normalized(after.balances),duplicatesSingleBooking:a.result.id===b.result.id&&(await d.state()).state.bookings.length===1,duplicateConflict:clash.status}}),
 item(58,'응답불명 재청구 차단·대조',{secondStatus:409,pendingBefore:1,pendingAfter:0},async()=>{
  const c=await fresh('U01');await c.command('registerCard',{scenario:'unknown'});await c.command('signup',{grade:'hope',consents:{terms:true,privacy:true,recurring:true,marketing:false}});
  const state=await c.state(),inv=state.state.invoices.find(i=>i.status==='unpaid');const second=await c.call('/review/api/command',{action:'payInvoice',invoiceId:inv.id,requestId:crypto.randomUUID()});
  const before=await document(c);await c.login('admin');await c.command('reconcilePayment');const after=await document(c);
  artifacts.unknown={before:before.meta.receipts.map(r=>({status:r.status,amount:r.amount})),after:after.meta.receipts.map(r=>({status:r.status,amount:r.amount})),attempts:after.state.attempts.map(a=>({status:a.status,invoiceId:a.invoiceId}))};
  return {secondStatus:second.status,pendingBefore:before.meta.receipts.filter(r=>r.status==='accepted_pending_query').length,pendingAfter:after.meta.receipts.filter(r=>r.status==='accepted_pending_query').length}}),
 item(59,'마지막1석 동시신청 범위',{sameWorkspaceMaxOne:true,crossWorkspaceGlobalMaxOne:true},async()=>{
  const c=await fresh(),v=await c.state();const replies=await Promise.all([c.call('/review/api/command',{action:'book',expectedTotal:v.quote.total,requestId:crypto.randomUUID()}),c.call('/review/api/command',{action:'book',expectedTotal:v.quote.total,requestId:crypto.randomUUID()})]);
  const same=(await c.state()).state.bookings.length;
  const a=await fresh(),b=await fresh();const cross=await Promise.all([purchase(a),purchase(b)]);artifacts.inventory={sameWorkspaceSuccessStatuses:replies.map(r=>r.status),sameWorkspaceBookings:same,separateWorkspaceConfirmed:cross.length,model:'real Worker CAS; inventories are private per review workspace; no shared supplier inventory'};
  return {sameWorkspaceMaxOne:same<=1,crossWorkspaceGlobalMaxOne:cross.length<=1}}),
 item(60,'취소 원수단·재고·세대 증거',{restored:true,rebook:true,staleDenied:409},async()=>{
  const c=await fresh(),bookingId=await purchase(c),r=await c.command('requestRefund',{bookingId,rooms:1});
  await c.login('finance');await c.command('reviewRefund',{refundId:r.result.id});await c.login('admin');await c.command('approveRefund',{refundId:r.result.id});await c.command('executeRefund',{refundId:r.result.id});await c.login('member');
  const restored=await c.state();const next=await purchase(c);artifacts.restoration=await snapshot(c);
  const seed=await c.call('/review/qa-fixtures/v1/seed',{runId:c.runId,generation:c.generation,dataset:'U04',sourceRevision:config.source_revision});assert(seed.status===200);
  const stale=await c.call('/review/api/command',{action:'registerCard',scenario:'success',requestId:crypto.randomUUID()});
  return {restored:restored.balances.DP===600000&&restored.balances.BP===150000&&restored.balances.SP===100000,rebook:next!==bookingId,staleDenied:stale.status}})
];
for(const job of jobs)await job();
emit(JSON.stringify({schema:'hope-actual-service-batch03/v1',source_revision:config.source_revision,scope:'actual_service_source_local_sqlite_simulator',virtual_clock:config.virtual_clock,items:results,transcript,artifacts,realPG:false,operational_authorization:false,customer_acceptance_approved:false}));db.close();
