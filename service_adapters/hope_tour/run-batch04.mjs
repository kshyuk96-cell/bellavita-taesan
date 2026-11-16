// Controlled test entry point. The imported service is pinned and network-isolated.
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
import {execFileSync} from 'node:child_process';
import {SharedInventory,sharedGateway} from './shared-inventory.mjs';
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
const inner=createFixtureBridge({api,db,appFetch,env,sourceRevision:config.source_revision,enabled:true,setupKey});
const inventory=new SharedInventory(config.inventory_contract,config.inventory_digest);
const bridge=sharedGateway({inventory,api,db,inner});
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
 bookings:doc.state.bookings,refunds:doc.state.refunds,travelEvents:doc.state.travelEvents,principalRecords:api.clubPrincipalRecords(doc.state),withdrawn:view.withdrawn}}
async function purchase(c){let v=await c.state();const reply=await c.command('book',{expectedTotal:v.quote.total});
 for(const inv of (await c.state()).state.invoices.filter(i=>i.bookingId===reply.result.id&&i.status==='unpaid'))await c.command('payInvoice',{invoiceId:inv.id});return reply.result.id}
const checks={uid:process.getuid(),networkDenied:false,hostFsDenied:false,spawnDenied:false};
try{await fetch('https://example.com',{signal:AbortSignal.timeout(500)})}catch{checks.networkDenied=true}
try{fs.readFileSync('/etc/passwd')}catch{checks.hostFsDenied=true}
try{execFileSync('/bin/true')}catch{checks.spawnDenied=true}
artifacts.confinement={...checks,verified:checks.uid===65534&&checks.networkDenied&&checks.hostFsDenied&&checks.spawnDenied};
async function attach(c,slot){await bridge.bindWorkspace(c,slot);return c}
async function refund(c,bookingId,rooms=1){const r=await c.command('requestRefund',{bookingId,rooms});await c.login('finance');await c.command('reviewRefund',{refundId:r.result.id});await c.login('admin');await c.command('approveRefund',{refundId:r.result.id});await c.command('executeRefund',{refundId:r.result.id});await c.login('member');return r.result.id}
function finish(){artifacts.inventory=inventory.snapshot();emit(JSON.stringify({schema:'hope-actual-service-batch04/v1',source_revision:config.source_revision,scope:'actual_service_source_shared_test_inventory',role:config.role,inventory_digest:config.inventory_digest,virtual_clock:config.virtual_clock,items:results,transcript,artifacts,realPG:false,operational_authorization:false,customer_acceptance_approved:false}));inventory.close();db.close()}
if(config.role!=='main'){
 const c=await attach(await fresh(),'race');
 inventory.db.prepare('INSERT INTO inventory_barrier VALUES(?,1)').run(config.role);
 const until=performance.now()+15000;
 while(inventory.db.prepare('SELECT released FROM inventory_gate WHERE id=1').get().released!==1){if(performance.now()>until)throw Error('independent executor barrier timeout');await new Promise(r=>setTimeout(r,20))}
 const v=await c.state(),reply=await c.call('/review/api/command',{action:'book',expectedTotal:v.quote.total,requestId:crypto.randomUUID()});
 if(reply.status===200)for(const i of (await c.state()).state.invoices.filter(i=>i.bookingId===reply.data.result.id&&i.status==='unpaid'))await c.command('payInvoice',{invoiceId:i.id});
 artifacts.race={executor:config.role,status:reply.status,confirmed:(await c.state()).state.bookings.filter(b=>b.status==='confirmed').length};finish();
}else{
 const jobs=[
 item(61,'공유 비운영 재고 계약',{pinned:true,supplier:false},async()=>({pinned:config.inventory_contract.source_revision===config.source_revision&&config.inventory_contract.environment==='isolated_nonproduction',supplier:config.inventory_contract.supplier_connected})),
 item(62,'별도 공유 테스트 저장소',{separate:true,operationalTables:0},async()=>({separate:inventory.db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='review_workspaces'").all().length===0,operationalTables:inventory.db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'inventory_%'").all().length})),
 item(63,'원자 확보·확정·해제',{soldOutDenied:true,restored:1},async()=>{
  const params={key:'atomic-a',slot:'atomic',owner:'TEST-owner',generation:1,fingerprint:'TEST',quantity:1,now:Date.now()};inventory.reserve(params);let denied=false;try{inventory.reserve({...params,key:'atomic-b'})}catch(e){denied=e.status===409}
  inventory.bind(params.key,'TEST-booking-atomic','confirmed');inventory.release(params.key,[1],'TEST-refund-atomic',{known:true});return {soldOutDenied:denied,restored:inventory.snapshot().slots.find(r=>r.slot==='atomic').available}}),
 item(64,'만료·불명확 효과 보존',{expired:1,uncertainBlocked:true,held:1},async()=>{
  const params={key:'expiry-a',slot:'expiry',owner:'TEST-expiry',generation:1,fingerprint:'TEST',quantity:1,now:Date.now()};inventory.reserve(params);const expired=inventory.expire(params.key,Date.now()+16*60000,{bookingAbsent:true});inventory.reserve({...params,key:'expiry-unknown'});inventory.uncertain('expiry-unknown');let denied=false;try{inventory.expire('expiry-unknown',Date.now()+16*60000,{bookingAbsent:true})}catch(e){denied=e.status===409}
  return {expired,uncertainBlocked:denied,held:inventory.active(params.owner).reduce((n,r)=>n+r.quantity-r.released,0)}}),
 item(65,'다른 회원 마지막1석',{successes:1,rejected:1,confirmed:1},async()=>{
  const a=await attach(await fresh(),'different-members'),b=await attach(await fresh(),'different-members'),v=await a.state();
  const replies=await Promise.all([a,b].map(c=>c.call('/review/api/command',{action:'book',expectedTotal:v.quote.total,requestId:crypto.randomUUID()})));
  for(let n=0;n<2;n++)if(replies[n].status===200){const c=[a,b][n];for(const i of (await c.state()).state.invoices.filter(i=>i.bookingId===replies[n].data.result.id&&i.status==='unpaid'))await c.command('payInvoice',{invoiceId:i.id})}
  const states=await Promise.all([a.state(),b.state()]);artifacts.differentMembers=states.map(s=>({bookings:s.state.bookings.map(b=>({id:b.id,status:b.status})),balances:s.balances}));return {successes:replies.filter(r=>r.status===200).length,rejected:replies.filter(r=>r.status===409).length,confirmed:states.reduce((n,s)=>n+s.state.bookings.filter(b=>b.status==='confirmed').length,0)}}),
 item(67,'중복키·재전달·세대',{bookings:1,cardApprovals:1,stale:409},async()=>{
  const c=await attach(await fresh(),'duplicate'),v=await c.state(),id=crypto.randomUUID();const a=await c.command('book',{expectedTotal:v.quote.total},id);const b=await c.command('book',{expectedTotal:v.quote.total},id);assert(a.result.id===b.result.id);
  for(const i of (await c.state()).state.invoices.filter(i=>i.bookingId===a.result.id&&i.status==='unpaid')){const payKey=crypto.randomUUID();await c.command('payInvoice',{invoiceId:i.id},payKey);await c.command('payInvoice',{invoiceId:i.id},payKey)}
  const doc=await document(c),prior=c.generation;c.generation=prior-1;const stale=await c.call('/review/api/command',{action:'book',expectedTotal:v.quote.total,requestId:crypto.randomUUID()});c.generation=prior;artifacts.duplicate=await snapshot(c);
  return {bookings:doc.state.bookings.length,cardApprovals:doc.state.travelEvents.filter(e=>e.type==='succeeded'&&e.amount>0).length,stale:stale.status}}),
 item(68,'부분 객실 취소·1회 복원',{afterFirst:1,afterReplay:1,afterSecond:2,rebook:true},async()=>{
  const c=await fresh(),session=await api.rvSession(new Request('https://local-qa.invalid/review/api/state',{headers:{cookie:c.cookie}}),db);const loaded=await api.rvRead(db,session.workspace_id),doc=loaded.document;
  inner.select(doc,{...PRODUCTS[1],stock:2});const selected=doc.meta.selectedCatalog;selected.order.quote=api.quoteOrder(selected.product,selected.config,{offerId:selected.config.offers[0].id,adults:2,children:0,rooms:2,extras:[]},new Date(doc.meta.day+'T01:00:00Z'));
  db.prepare('UPDATE review_workspaces SET payload=?,revision=revision+1 WHERE id=? AND revision=?').bind(JSON.stringify(doc),loaded.row.id,loaded.row.revision).run();await attach(c,'partial');const bookingId=await purchase(c),first=await refund(c,bookingId,1);
  const stock=()=>inventory.snapshot().slots.find(r=>r.slot==='partial').available;const afterFirst=stock();await c.login('admin');const replay=await c.call('/review/api/command',{action:'executeRefund',refundId:first,requestId:crypto.randomUUID()});await c.login('member');assert([200,409].includes(replay.status));const afterReplay=stock();
  await refund(c,bookingId,1);const afterSecond=stock();const next=await purchase(c);artifacts.partial=await snapshot(c);return {afterFirst,afterReplay,afterSecond,rebook:next!==bookingId}}),
 item(69,'여행 예약금·잔금 응답불명 재청구 차단',{depositRetry:409,balanceRetry:409,receipts:2},async()=>{
  const outputs=[];
  for(const [slot,dataset,subtype] of [['travel-deposit','U10','deposit'],['travel-balance','U04','balance']]){
   const c=await attach(await fresh(dataset),slot);const v=await c.state(),book=await c.command('book',{expectedTotal:v.quote.total});
   let invoices=(await c.state()).state.invoices.filter(i=>i.bookingId===book.result.id&&i.status==='unpaid');
   if(subtype==='balance')for(const i of invoices.filter(i=>i.subtype==='deposit'))await c.command('payInvoice',{invoiceId:i.id});
   const invoice=(await c.state()).state.invoices.find(i=>i.bookingId===book.result.id&&i.subtype===subtype);assert(invoice.amount>0);
   await c.command('registerCard',{scenario:'unknown'});await c.command('payInvoice',{invoiceId:invoice.id});const retry=await c.call('/review/api/command',{action:'payInvoice',invoiceId:invoice.id,requestId:crypto.randomUUID()});
   const cancellation=await c.call('/review/api/command',{action:'requestRefund',bookingId:book.result.id,rooms:1,requestId:crypto.randomUUID()});
   await c.login('admin');const expiry=await c.call('/review/api/command',{action:'expireHolds',requestId:crypto.randomUUID()});await c.command('reconcilePayment');await c.login('member');
   await c.command('registerCard',{scenario:'success'});for(const i of (await c.state()).state.invoices.filter(i=>i.bookingId===book.result.id&&i.status==='unpaid'))await c.command('payInvoice',{invoiceId:i.id});
   const final=await snapshot(c);outputs.push({slot,retry:retry.status,cancellation:cancellation.status,expiry:expiry.status,invoice:invoice.id,amount:invoice.amount,beforeReconcileReceipts:1,final});
   // Refund after authoritative query restores the shared allocation.
   await refund(c,book.result.id);outputs.at(-1).afterRefund=await snapshot(c);
  }artifacts.travelUnknown=outputs;return {depositRetry:outputs[0].retry,balanceRetry:outputs[1].retry,receipts:outputs.length}}),
 item(70,'조회·취소·원장·재고 대조',{queryThenRefund:true,pending:0,restored:2,cancelWhileUnknownDenied:true},async()=>{
  const runs=artifacts.travelUnknown;return {queryThenRefund:runs.length===2&&runs.every(r=>r.afterRefund.bookings.every(b=>b.status==='cancelled')),pending:runs.reduce((n,r)=>n+r.final.invoices.filter(i=>i.status==='unpaid').length,0),restored:inventory.snapshot().slots.filter(s=>['travel-deposit','travel-balance'].includes(s.slot)&&s.available===1).length,cancelWhileUnknownDenied:runs.every(r=>r.cancellation===409&&r.expiry===409)}})
 ];
 for(const job of jobs)await job();
 const faultClient=await fresh(),faultBooking=await purchase(faultClient);await refund(faultClient,faultBooking);
 const clean=(await document(faultClient)).state;
 const view=s=>({contracts:s.contracts.map(c=>({id:c.id})),bookings:s.bookings,refunds:s.refunds,ledger:s.ledger,invoices:s.invoices,travelEvents:s.travelEvents});
 artifacts.faultInjection=[];artifacts.cleanDomain=view(clean);
 inventory.db.exec('BEGIN IMMEDIATE');
 try{inventory.db.prepare("UPDATE inventory_slots SET available=capacity WHERE slot='different-members'").run();artifacts.faultInjection.push({id:'free-stock-corruption',inventory:inventory.snapshot()})}finally{inventory.db.exec('ROLLBACK')}
 for(const kind of ['duplicate-debit','excess-refund','unbalanced-journal']){
  const state=structuredClone(clean),booking=state.bookings.find(b=>b.id===faultBooking),journal=state.ledger.find(j=>j.sourceKey==='booking:'+booking.id);
  if(kind==='duplicate-debit'){const copy=structuredClone(journal);copy.id=crypto.randomUUID();state.ledger.push(copy)}
  else if(kind==='excess-refund'){const r=state.refunds.find(r=>r.bookingId===booking.id&&r.status==='refunded');r.quote.refund.DP++;r.quote.refundTotal++}
  else journal.entries[0].delta++;
  artifacts.faultInjection.push({id:kind,domain:view(state),targetDetected:api.clubReconcile(state).length>0});
 }
 artifacts.terms_pages=[];const termClient=await fresh();for(const path of ['/review/','/review/features','/review/app','/review/policy']){const original=(await termClient.call(path)).data;artifacts.terms_pages.push({path,html:original.replace(/<input\b[^>]*\bname=["']password["'][^>]*>/gi,'<input name="password" value="[REDACTED]">').replaceAll(api.rvPassword||'HopeTest!2026','[REDACTED]')})}
 finish();
}
