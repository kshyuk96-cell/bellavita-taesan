// TEST-only controller. Query never replays a native command or releases on time alone.
import {sharedGateway} from './shared-inventory.mjs';
const fail=(message,status=409)=>Object.assign(Error(message),{status});
const stable=v=>Array.isArray(v)?v.map(stable):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,stable(v[k])])):v;
const hash=async v=>[...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(v)))].map(n=>n.toString(16).padStart(2,'0')).join('');
export function recoveryGateway({inventory,api,db,inner,config,setupKey}){
 const sql=inventory.db,task=config.business_contract.task,source=config.source_revision,executor=config.execution_id;
 sql.prepare("INSERT INTO recovery_executors(id,task,source,status) VALUES(?,?,?,'running')").run(executor,task,source);
 const sync=sharedGateway({inventory,api,db,inner}).sync;
 const bindingFor=owner=>sql.prepare('SELECT * FROM recovery_bindings WHERE workspace=?').get(owner);
 async function context(request){const session=await api.rvSession(request,db);if(!session)throw fail('actual TEST session required',401);const loaded=await api.rvRead(db,session.workspace_id);return {session,...loaded}}
 function checkBinding(binding,doc,request){
  if(!binding||binding.task!==task||binding.source!==source||binding.contract_digest!==config.business_digest||request.headers.get('x-hope-qa-generation')!==String(binding.generation))throw fail('source/task/generation binding mismatch');
  const run=db.sql.prepare('SELECT * FROM qa_fixture_runs WHERE workspace=?').get(binding.workspace);
  if(!run||run.generation!==binding.generation||run.source!==source||doc.meta.dataset?.sourceRevision!==source)throw fail('business generation changed');
 }
 async function bindWorkspace(client,slot,label){
  const {session,document}=await context(new Request('https://local-qa.invalid/review/api/state',{headers:{cookie:client.cookie}}));
  const q=api.rvSelectedCatalog(document).order.quote,res=inventory.contract.resource;
  if(q.productId!==res.product_id||q.offer.date!==res.departure||q.offer.room!==res.room||!inventory.contract.slots[slot]||slot!==label)throw fail('fixed TEST resource required');
  sql.prepare('INSERT INTO recovery_bindings(workspace,label,task,source,contract_digest,generation,run_id,slot,book_request) VALUES(?,?,?,?,?,?,?,?,?)').run(session.workspace_id,label,task,source,config.business_digest,client.generation,client.runId,slot,crypto.randomUUID());return bindingFor(session.workspace_id);
 }
 async function resume(label,role='member'){
  const binding=sql.prepare('SELECT * FROM recovery_bindings WHERE label=?').get(label);
  if(!binding||binding.task!==task||binding.source!==source||binding.contract_digest!==config.business_digest)throw fail('fixture binding missing');
  const account=api.rvAccounts.find(a=>a.username===role+'@hope.test');if(!account)throw fail('fixed fixture role required',403);
  const token=[...crypto.getRandomValues(new Uint8Array(32))].map(n=>n.toString(16).padStart(2,'0')).join('');
  // Approved TEST fixture auth bootstrap: persist only the native session hash.
  db.sql.prepare('INSERT INTO review_sessions VALUES(?,?,?,?,?)').run(await hash(token),binding.workspace,account.username,new Date().toISOString(),new Date(Date.now()+7*86400000).toISOString());
  return {cookie:'hope_review='+token,runId:binding.run_id,generation:binding.generation,binding};
 }
 function phase(key,value){sql.prepare('UPDATE recovery_operations SET phase=? WHERE key=?').run(value,key)}
 function crash(point,key){phase(key,point);if(config.role==='crash-'+point)process.exit(86)}
 async function observe(binding,doc,row,requestId){
  const key=binding.workspace+':'+binding.generation+':'+requestId,op=sql.prepare('SELECT * FROM recovery_operations WHERE key=?').get(key);
  if(!op||op.task!==task||op.source!==source||op.workspace!==binding.workspace||op.generation!==binding.generation)throw fail('operation not owned by this binding',404);
  const stopped=sql.prepare('SELECT * FROM recovery_executors WHERE id=?').get(op.executor);
  const proof=stopped?.stop_receipt?JSON.parse(stopped.stop_receipt):null;
  const fenced=stopped?.status==='stopped'&&proof?.container_absent===true&&proof.process_exited===true&&proof.source_revision===source&&proof.task===task;
  const record=doc.meta.requests.find(r=>r.id===requestId),command=JSON.parse(op.command);
  const matching=doc.state.bookings.filter(b=>b.requestKey==='review-book:'+requestId);
  if(record){
   if(record.fingerprint!==op.fingerprint||matching.length!==1||record.result.id!==matching[0].id)throw fail('native request/booking fingerprint mismatch');
   const booking=matching[0],selected=api.rvSelectedCatalog(doc).order.quote,res=inventory.contract.resource;
   if(booking.total!==command.expectedTotal||booking.productId!==res.product_id||booking.departure!==res.departure||booking.rooms!==selected.selection.rooms)throw fail('native resource/amount mismatch');
   const pending=doc.meta.receipts.some(r=>r.kind==='travel'&&r.status==='accepted_pending_query'&&doc.state.invoices.some(i=>i.id===r.invoiceId&&i.bookingId===booking.id));
   return {key,op,booking,record,public:{state:pending?'payment_pending_query':'existing',bookingId:booking.id,bookingStatus:booking.status,executorStopped:fenced,revision:row.revision,replayAllowed:false}};
  }
  return {key,op,public:{state:fenced&&matching.length===0&&row.revision===op.revision_before?'known_absent':'needs_reconciliation',executorStopped:fenced,revision:row.revision,replayAllowed:false}};
 }
 async function handle(request){
  const path=new URL(request.url).pathname;
  try{
   if(path==='/review/form/command')throw fail('bound TEST controller accepts JSON command path only',405);
   if(path.startsWith('/review/qa-recovery/v1/')){
    if(request.method!=='POST'||!['/review/qa-recovery/v1/query','/review/qa-recovery/v1/reconcile'].includes(path))throw fail('fixed recovery route required',405);
    const {session,document,row}=await context(request),binding=bindingFor(session.workspace_id);checkBinding(binding,document,request);
    const raw=await request.text();if(raw.length>4096)throw fail('bounded recovery body required',413);const body=JSON.parse(raw);if(!body||typeof body!=='object'||Array.isArray(body)||Object.keys(body).length!==1||!/^[a-f0-9-]{36}$/.test(body.requestId))throw fail('only existing request ID accepted',400);
    const observation=await observe(binding,document,row,body.requestId);
    if(path.endsWith('/query'))return Response.json(observation.public);
    if(session.account.role!=='owner'||request.headers.get('x-hope-qa-key')!==setupKey)throw fail('owner fixture reconciliation required',403);
    if(!observation.public.executorStopped)throw fail('verified stopped executor required');
    const lease=inventory.get(observation.key);
    if(observation.public.state==='known_absent'){
     if(lease&&lease.status!=='released'){if(lease.status==='uncertain')inventory.bind(lease.key,'known-absent:'+body.requestId,'held');inventory.release(lease.key,Array.from({length:lease.quantity},(_,i)=>i+1),'known-absent:'+body.requestId,{known:true})}
     phase(observation.key,'recovered_absent');
    }else if(observation.public.state==='existing'||observation.public.state==='payment_pending_query'){
     if(!lease)throw fail('native booking has no approved shared allocation');
     if(lease.status!=='released'){inventory.bind(lease.key,observation.booking.id,observation.booking.status==='confirmed'?'confirmed':'held');await sync(binding.workspace,document)}
     else if(observation.booking.status!=='cancelled'&&observation.booking.status!=='expired')throw fail('released allocation conflicts with active booking');
     phase(observation.key,'recovered_existing');
    }else throw fail('effects remain uncertain; no automatic release');
    sql.prepare('UPDATE recovery_operations SET result=? WHERE key=?').run(JSON.stringify(observation.public),observation.key);return Response.json(observation.public);
   }
   if(path==='/review/qa-fixtures/v1/seed'){
    const {session}=await context(request);
    if(inventory.active(session.workspace_id).length||sql.prepare("SELECT key FROM recovery_operations WHERE workspace=? AND phase NOT IN ('recovered_absent','recovered_existing','completed')").get(session.workspace_id))throw fail('resolve active operation before reseed');
   }
   if(path!=='/review/api/command')return inner.handle(request);
   const {session,document,row}=await context(request),raw=await request.clone().text();if(raw.length>4096)throw fail('bounded command body required',413);const body=JSON.parse(raw),binding=bindingFor(session.workspace_id);if(!body||typeof body!=='object'||Array.isArray(body))throw fail('object command required',400);
   if(!binding){if(body.action==='book')throw fail('persistent TEST binding required');return inner.handle(request)}
   checkBinding(binding,document,request);
   if(body.action!=='book'){const response=await inner.handle(request);const next=(await api.rvRead(db,session.workspace_id)).document;await sync(session.workspace_id,next);return response}
   if(session.account.role!=='member')throw fail('member role required',403);
   if(Object.keys(body).sort().join(',')!=='action,expectedTotal,requestId'||!/^[a-f0-9-]{36}$/.test(body.requestId))throw fail('fixed booking input required',400);
   const q=api.rvSelectedCatalog(document).order.quote,res=inventory.contract.resource;
   if(q.productId!==res.product_id||q.offer.date!==res.departure||q.offer.room!==res.room)throw fail('resource contract changed');
   const key=binding.workspace+':'+binding.generation+':'+body.requestId,fingerprint=await hash(JSON.stringify({account:session.account.username,command:stable(body)}));
   const prior=sql.prepare('SELECT * FROM recovery_operations WHERE key=?').get(key);
   if(prior){
    if(prior.fingerprint!==fingerprint)throw fail('request key body conflict');
    if(prior.phase==='completed'||prior.phase==='recovered_existing'){
     const observed=await observe(binding,document,row,body.requestId);if(observed.record)return Response.json({result:observed.record.result,duplicate:true,revision:row.revision});
    }throw fail('existing operation must be queried; never replay');
   }
   sql.prepare('INSERT INTO recovery_operations(key,workspace,task,source,generation,request_id,command,fingerprint,executor,phase,revision_before) VALUES(?,?,?,?,?,?,?,?,?,?,?)').run(key,binding.workspace,task,source,binding.generation,body.requestId,JSON.stringify(body),fingerprint,executor,'prepared',row.revision);
   crash('before-hold',key);
   const lease=inventory.reserve({key,slot:binding.slot,owner:binding.workspace,generation:binding.generation,fingerprint,quantity:q.selection.rooms,now:Date.now()});crash('after-hold',key);
   inventory.uncertain(key);crash('before-store',key);
   const response=await inner.handle(request);crash('after-store',key);
   const next=await api.rvRead(db,session.workspace_id),observed=await observe(binding,next.document,next.row,body.requestId);
   if(observed.booking){inventory.bind(key,observed.booking.id,'held');await sync(binding.workspace,next.document);phase(key,'completed')}
   // Unknown/5xx/nonrecorded errors remain fenced; only stopped-executor reconciliation can release.
   crash('before-response',key);phase(key,observed.booking?'completed':'needs_reconciliation');return response;
  }catch(e){return Response.json({error:e.status?e.message:'persistent TEST operation uncertain',scope:'persistent-test-recovery'}, {status:e.status||503})}
 }
 return {handle,bindWorkspace,resume,bindingFor,sync};
}
