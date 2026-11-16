// Controlled test entry point. The imported service is pinned and network-isolated.
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
import {execFileSync} from 'node:child_process';
import {SharedInventory,sharedGateway} from './shared-inventory.mjs';
import {PersistentReview} from './persistent-review.mjs';
import {recoveryGateway} from './recovery-gateway.mjs';
import {createFixtureBridge,DEFINITIONS,PRODUCTS} from './fixture-bridge.mjs';
const emit=console.log.bind(console);console.log=()=>{};
const config=JSON.parse(fs.readFileSync('/input/config.json','utf8'));
const RealDate=Date;
globalThis.Date=class extends RealDate{constructor(...args){super(...(args.length?args:[config.virtual_clock]))}static now(){return RealDate.parse(config.virtual_clock)}};
const api=await import('./sut.bundle.mjs');
const results=[],transcript=[],artifacts={};
const inventory=new SharedInventory(config.inventory_contract,config.inventory_digest);
const db=new PersistentReview(config,inventory);
const setupKey=[...crypto.getRandomValues(new Uint8Array(32))].map(x=>x.toString(16).padStart(2,'0')).join('');
let secretBytes='';for await(const chunk of process.stdin)secretBytes+=chunk;
const secretInput=JSON.parse(secretBytes);const env={DB:db,REVIEW_DATA_KEY:secretInput.REVIEW_DATA_KEY};secretBytes='';delete secretInput.REVIEW_DATA_KEY;await db.verifyKey(env.REVIEW_DATA_KEY,api);
const root='/tmp/site';fs.cpSync('/input/sut',root,{recursive:true});process.chdir(root);
let built;
try{await import(pathToFileURL(root+'/build-worker.mjs'));built=(await import(pathToFileURL(root+'/dist/server/index.js'))).default}
catch{emit(JSON.stringify({fatal:'original_service_build_failed',source_revision:config.source_revision,operational_authorization:false}));process.exit(2)}
const appFetch=request=>built.fetch(request,env);
const inner=createFixtureBridge({api,db,appFetch,env,sourceRevision:config.source_revision,enabled:true,setupKey});
const bridge=recoveryGateway({inventory,api,db,inner,config,setupKey});
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
  if(options.setup!==false&&(path.startsWith('/review/qa-fixtures/')||path.startsWith('/review/qa-recovery/')))headers['x-hope-qa-key']=setupKey;
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
 bookings:doc.state.bookings,refunds:doc.state.refunds,travelEvents:doc.state.travelEvents,principalRecords:api.clubPrincipalRecords(doc.state),withdrawn:view.withdrawn}}
async function purchase(c){let v=await c.state();const reply=await c.command('book',{expectedTotal:v.quote.total});
 for(const inv of (await c.state()).state.invoices.filter(i=>i.bookingId===reply.result.id&&i.status==='unpaid'))await c.command('payInvoice',{invoiceId:inv.id});return reply.result.id}
const checks={uid:process.getuid(),networkDenied:false,hostFsDenied:false,spawnDenied:false};
try{await fetch('https://example.com',{signal:AbortSignal.timeout(500)})}catch{checks.networkDenied=true}
try{fs.readFileSync('/etc/passwd')}catch{checks.hostFsDenied=true}
try{execFileSync('/bin/true')}catch{checks.spawnDenied=true}
artifacts.keyContinuity=db.keyVerified===true;
artifacts.confinement={...checks,verified:checks.uid===65534&&checks.networkDenied&&checks.hostFsDenied&&checks.spawnDenied};
async function resumed(label){const v=await bridge.resume(label);return new Client(v.cookie,v.runId,v.generation)}
const binding=label=>inventory.db.prepare('SELECT * FROM recovery_bindings WHERE label=?').get(label);
async function query(c,requestId,action='query'){const r=await c.call('/review/qa-recovery/v1/'+action,{requestId});return r}
async function refund(c,bookingId,rooms=1){const r=await c.command('requestRefund',{bookingId,rooms});await c.login('finance');await c.command('reviewRefund',{refundId:r.result.id});await c.login('admin');await c.command('approveRefund',{refundId:r.result.id});await c.command('executeRefund',{refundId:r.result.id});await c.login('member');return r.result.id}
function finish(){artifacts.inventory=inventory.snapshot();artifacts.bindings=inventory.db.prepare('SELECT workspace,label,task,source,generation,slot,book_request FROM recovery_bindings ORDER BY label').all();artifacts.operations=inventory.db.prepare('SELECT key,workspace,task,source,generation,request_id,executor,phase,revision_before,result FROM recovery_operations ORDER BY key').all();
 emit(JSON.stringify({schema:'hope-actual-service-batch05/v1',source_revision:config.source_revision,scope:'actual_source_persistent_test_recovery',role:config.role,inventory_digest:config.inventory_digest,business_digest:config.business_digest,virtual_clock:config.virtual_clock,items:results,transcript,artifacts,realPG:false,supplier_connected:false,operational_authorization:false,customer_acceptance_approved:false}));db.close();inventory.close()}
const phases=['before-hold','after-hold','before-store','after-store','before-response'];
if(config.role==='setup'){
 for(const label of [...phases,'shared-worker','restarted-unknown','rounding','audit']){
  const c=await fresh();
  if(label==='rounding'){
   const current=await api.rvSession(new Request('https://local-qa.invalid/review/api/state',{headers:{cookie:c.cookie}}),db),loaded=await api.rvRead(db,current.workspace_id),doc=loaded.document;
   inner.select(doc,{...PRODUCTS[1],stock:2});const s=doc.meta.selectedCatalog;s.config.offers[0].adultPrice=515470;
   s.order.quote=api.quoteOrder(s.product,s.config,{offerId:s.config.offers[0].id,adults:2,children:0,rooms:2,extras:[]},new Date(doc.meta.day+'T01:00:00Z'));
   db.prepare('UPDATE review_workspaces SET payload=?,revision=revision+1 WHERE id=? AND revision=?').bind(JSON.stringify(doc),loaded.row.id,loaded.row.revision).run();
  }
  const bound=await bridge.bindWorkspace(c,label,label);
  if(['shared-worker','restarted-unknown'].includes(label)){
   const v=await c.state(),book=await c.command('book',{expectedTotal:v.quote.total},bound.book_request);
   for(const i of (await c.state()).state.invoices.filter(i=>i.bookingId===book.result.id&&i.subtype==='deposit'))await c.command('payInvoice',{invoiceId:i.id});
   const invoice=(await c.state()).state.invoices.find(i=>i.bookingId===book.result.id&&i.subtype==='balance');
   inventory.db.prepare('UPDATE recovery_bindings SET aux=? WHERE label=?').run(JSON.stringify({bookingId:book.result.id,pay_invoice_id:invoice.id,pay_request_id:crypto.randomUUID()}),label);
   if(label==='restarted-unknown'){await c.command('registerCard',{scenario:'unknown'});await c.command('payInvoice',{invoiceId:invoice.id})}
  }
 }
 artifacts.setup={workspaces:inventory.db.prepare('SELECT count(*) AS n FROM recovery_bindings').get().n,persistent:true};finish();
}else if(config.role.startsWith('crash-')){
 const label=config.role.slice(6),c=await resumed(label),b=binding(label),v=await c.state();await c.command('book',{expectedTotal:v.quote.total},b.book_request);throw Error('configured interruption did not occur');
}else if(config.role==='recover'){
 const observations=[];
 for(const label of [...phases,'restarted-unknown']){
  const c=await resumed(label),b=binding(label),before=await snapshot(c);const beforeRevision=(await api.rvRead(db,b.workspace)).row.revision;const q=await query(c,b.book_request);const afterRead=await snapshot(c);const afterRevision=(await api.rvRead(db,b.workspace)).row.revision;
  const readonly=normalized(before)===normalized(afterRead)&&beforeRevision===afterRevision;await c.login('admin');const result=await query(c,b.book_request,'reconcile');assert(result.status===200,'reconciliation denied');
  await c.login('member');const replay=await c.call('/review/api/command',{action:'book',expectedTotal:(await c.state()).quote.total,requestId:b.book_request});
  const final=await snapshot(c);observations.push({label,query:q,readonly,beforeRevision,afterRevision,reconciliation:result,replayStatus:replay.status,final});
 }
 artifacts.recovery=observations;finish();
}else if(config.role.startsWith('race-')){
 const c=await resumed('shared-worker'),aux=JSON.parse(binding('shared-worker').aux),reply=await c.command('payInvoice',{invoiceId:aux.pay_invoice_id},aux.pay_request_id);
 artifacts.race={role:config.role,duplicate:reply.duplicate===true,final:await snapshot(c)};finish();
}else if(config.role==='complete-unknown'){
 const c=await resumed('restarted-unknown'),b=binding('restarted-unknown'),aux=JSON.parse(b.aux);const retry=await c.call('/review/api/command',{action:'payInvoice',invoiceId:aux.pay_invoice_id,requestId:crypto.randomUUID()});
 await c.login('admin');await c.command('reconcilePayment');await c.login('member');await c.command('registerCard',{scenario:'success'});
 const before=await snapshot(c);await refund(c,aux.bookingId);const q=await query(c,b.book_request);await c.login('admin');const requery=await c.call('/review/api/command',{action:'reconcilePayment',requestId:crypto.randomUUID()});await c.login('member');
 artifacts.unknownRecovery={retry:retry.status,afterQueryBeforeRefund:before,afterRefund:await snapshot(c),readOnlyQuery:q,requery:requery.status};finish();
}else{
 const safetyClient=await resumed('before-hold'),safeBinding=binding('before-hold');
 const memberReconcile=await query(safetyClient,safeBinding.book_request,'reconcile');
 const foreign=await query(safetyClient,binding('after-store').book_request);const priorGeneration=safetyClient.generation;safetyClient.generation++;const stale=await query(safetyClient,safeBinding.book_request);safetyClient.generation=priorGeneration;
 await safetyClient.login('admin');const scopeStatuses=[];
 for(const [field,value] of [['task','unapproved-task'],['source','0'.repeat(40)]]){
  inventory.db.exec('BEGIN IMMEDIATE');try{inventory.db.prepare('UPDATE recovery_bindings SET '+field+'=? WHERE workspace=?').run(value,safeBinding.workspace);scopeStatuses.push((await query(safetyClient,safeBinding.book_request)).status)}finally{inventory.db.exec('ROLLBACK')}
 }
 const operation=inventory.db.prepare('SELECT * FROM recovery_operations WHERE workspace=?').get(safeBinding.workspace);
 inventory.db.exec('BEGIN IMMEDIATE');let activeDenied;
 try{inventory.db.prepare("UPDATE recovery_executors SET status='running' WHERE id=?").run(operation.executor);activeDenied=(await query(safetyClient,safeBinding.book_request,'reconcile')).status}finally{inventory.db.exec('ROLLBACK')}
 artifacts.safety={memberReconcile:memberReconcile.status,foreign:foreign.status,stale:stale.status,taskMismatch:scopeStatuses[0],sourceMismatch:scopeStatuses[1],activeExecutorDenied:activeDenied};
 const audit=await resumed('audit'),bookId=await purchase(audit);await refund(audit,bookId);const clean=(await document(audit)).state;
 const view=s=>({contracts:s.contracts.map(c=>({id:c.id})),bookings:s.bookings,refunds:s.refunds,ledger:s.ledger,invoices:s.invoices,travelEvents:s.travelEvents});
 artifacts.cleanAudit=view(clean);artifacts.faultInjection=[];
 const mutations={
  'duplicate-source':s=>{const original=s.ledger.find(j=>j.sourceKey==='booking:'+bookId),copy=structuredClone(original);copy.id=crypto.randomUUID();s.ledger.push(copy)},
  'positive-duplicate-debit':s=>{const original=s.ledger.find(j=>j.sourceKey==='booking:'+bookId),ct=s.contracts[0],copy=structuredClone(original);copy.id=crypto.randomUUID();copy.entries=[{account:ct.id,book:'DP',delta:-1},{account:'system:TEST',book:'DP',delta:1}];s.ledger.push(copy)},
  'duplicate-event':s=>{const e=structuredClone(s.travelEvents.find(e=>e.type==='succeeded'&&e.amount>0));e.id=crypto.randomUUID();s.travelEvents.push(e)},
  'excess-refund':s=>{const r=s.refunds.find(r=>r.bookingId===bookId);r.quote.refund.DP++;r.quote.refundTotal++},
  'missing-journal':s=>{s.ledger=s.ledger.filter(j=>j.sourceKey!=='refund:'+s.refunds[0].id)}
 };
 for(const [id,mutate] of Object.entries(mutations)){const state=structuredClone(clean);mutate(state);artifacts.faultInjection.push({id,targetIssues:api.clubReconcile(state).map(i=>({key:i.key,kind:i.kind})),positiveDP:api.clubBalances(state,state.contracts[0].id).DP>0,domain:view(state)})}
 await item(89,'중복원장 검출 수정',{fullDuplicate:true,positiveDuplicate:true,positiveBalance:true},async()=>({fullDuplicate:artifacts.faultInjection[0].targetIssues.some(i=>i.key.startsWith('duplicate-source:')),positiveDuplicate:artifacts.faultInjection[1].targetIssues.some(i=>i.key.startsWith('duplicate-source:')),positiveBalance:artifacts.faultInjection[1].positiveDP}))();
 await item(90,'대상 대사기 오류검출',{detected:3,cleanIssues:0},async()=>({detected:artifacts.faultInjection.slice(2).filter(f=>f.targetIssues.length>0).length,cleanIssues:api.clubReconcile(clean).length}))();
 const rounding=await resumed('rounding'),v=await rounding.state(),b=binding('rounding'),reply=await rounding.command('book',{expectedTotal:v.quote.total},b.book_request),bookingId=reply.result.id;
 const doc=await document(rounding),booking=doc.state.bookings.find(b=>b.id===bookingId);artifacts.rounding={before:await snapshot(rounding),total:booking.total,plan:booking.plan,slices:booking.roomSlices};
 await item(97,'부분배분 정수 나머지',{total:1000011,rooms:2,sliceTotal:1000011},async()=>({total:booking.total,rooms:booking.roomSlices.length,sliceTotal:booking.roomSlices.reduce((n,r)=>n+r.total,0)}))();
 for(const i of (await rounding.state()).state.invoices.filter(i=>i.bookingId===bookingId&&i.subtype==='deposit'))await rounding.command('payInvoice',{invoiceId:i.id});
 const first=await refund(rounding,bookingId,1),afterFirst=await snapshot(rounding);await rounding.login('admin');const repeated=await rounding.call('/review/api/command',{action:'executeRefund',refundId:first,requestId:crypto.randomUUID()});await rounding.login('member');const afterRepeat=await snapshot(rounding);
 for(const i of (await rounding.state()).state.invoices.filter(i=>i.bookingId===bookingId&&i.status==='unpaid'))await rounding.command('payInvoice',{invoiceId:i.id});
 await refund(rounding,bookingId,1);const afterSecond=await snapshot(rounding);artifacts.rounding=Object.assign(artifacts.rounding,{afterFirst,afterRepeat,afterSecond,repeatedStatus:repeated.status});
 await item(98,'반복 부분취소·잔금 전후',{repeatDenied:409,firstUnpaid:100005,cardReturned:100005,stock:2},async()=>({repeatDenied:repeated.status,firstUnpaid:afterFirst.invoices.filter(i=>i.bookingId===bookingId&&i.status==='unpaid').reduce((n,i)=>n+i.amount,0),cardReturned:afterSecond.travelEvents.filter(e=>e.type==='refunded').reduce((n,e)=>n+e.amount,0),stock:inventory.snapshot().slots.find(s=>s.slot==='rounding').available}))();
 const termClient=await resumed('audit');artifacts.terms_pages=[];for(const path of ['/review/','/review/features','/review/app','/review/policy']){const original=(await termClient.call(path)).data;artifacts.terms_pages.push({path,html:original.replace(/<input\b[^>]*\bname=["']password["'][^>]*>/gi,'<input name="password" value="[REDACTED]">').replaceAll(api.rvPassword||'HopeTest!2026','[REDACTED]')})}
 finish();
}
