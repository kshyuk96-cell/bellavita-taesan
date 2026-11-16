// Dedicated TEST inventory only. No caller-supplied SQL, paths or supplier network.
import {DatabaseSync} from 'node:sqlite';
const fail=(message,status=409)=>Object.assign(Error(message),{status});
export class SharedInventory {
 constructor(contract,pin){
  if(contract.environment!=='isolated_nonproduction'||contract.operational_tables!==false||contract.supplier_connected!==false)throw fail('TEST inventory contract required',403);
  this.contract=contract;this.db=new DatabaseSync('/exchange/inventory.sqlite');this.db.exec('PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON; PRAGMA max_page_count=8192');
  if(this.db.prepare('SELECT contract_digest FROM inventory_meta WHERE id=1').get()?.contract_digest!==pin)throw fail('inventory contract mismatch',403);
 }
 tx(fn){this.db.exec('BEGIN IMMEDIATE');try{const r=fn();this.db.exec('COMMIT');return r}catch(e){this.db.exec('ROLLBACK');throw e}}
 event(kind,key,detail){this.db.prepare('INSERT INTO inventory_events(kind,lease,detail) VALUES(?,?,?)').run(kind,key,JSON.stringify(detail))}
 get(key){return this.db.prepare('SELECT * FROM inventory_leases WHERE key=?').get(key)}
 reserve({key,slot,owner,generation,fingerprint,quantity,now}){
  if(!this.contract.slots[slot]||typeof owner!=='string'||owner.length>100||typeof key!=='string'||key.length>250||typeof fingerprint!=='string'||fingerprint.length>2000||!Number.isInteger(generation)||generation<1||!Number.isInteger(quantity)||quantity<1||quantity>100||!Number.isSafeInteger(now))throw fail('fixed inventory fields required',400);
  return this.tx(()=>{
   const old=this.get(key);if(old){if(old.owner!==owner||old.generation!==generation||old.fingerprint!==fingerprint||old.slot!==slot||old.quantity!==quantity)throw fail('inventory idempotency conflict');if(old.status==='released')throw fail('released request cannot replay');return old}
   const changed=this.db.prepare('UPDATE inventory_slots SET available=available-? WHERE slot=? AND available>=?').run(quantity,slot,quantity);
   if(changed.changes!==1)throw fail('TEST shared inventory sold out');
   this.db.prepare('INSERT INTO inventory_leases(key,slot,owner,generation,fingerprint,quantity,status,expires) VALUES(?,?,?,?,?,?,?,?)').run(key,slot,owner,generation,fingerprint,quantity,'held',now+15*60000);
   this.event('reserved',key,{quantity});return this.get(key);
  });
 }
 uncertain(key){return this.tx(()=>{const r=this.get(key);if(!r||r.status==='released')throw fail('active lease required');this.db.prepare("UPDATE inventory_leases SET status='uncertain' WHERE key=?").run(key);this.event('uncertain',key,{});return this.get(key)})}
 bind(key,booking,status){
  if(!['held','confirmed'].includes(status)||typeof booking!=='string'||!booking)throw fail('authoritative booking proof required');
  return this.tx(()=>{const r=this.get(key);if(!r||r.status==='released'||r.booking&&r.booking!==booking)throw fail('booking binding mismatch');this.db.prepare('UPDATE inventory_leases SET booking=?,status=? WHERE key=?').run(booking,status,key);this.event('bound',key,{booking,status});return this.get(key)})
 }
 release(key,units,receipt,{known=false}={}){
  if(known!==true||!Array.isArray(units)||!units.length||new Set(units).size!==units.length||typeof receipt!=='string'||!receipt)throw fail('authoritative release proof required');
  return this.tx(()=>{const r=this.get(key);if(!r||r.status==='uncertain')throw fail('uncertain lease requires reconciliation');let count=0;
   for(const unit of units){if(!Number.isInteger(unit)||unit<1||unit>r.quantity)throw fail('release unit mismatch');const prior=this.db.prepare('SELECT receipt FROM inventory_releases WHERE lease=? AND unit=?').get(key,unit);if(prior){if(prior.receipt!==receipt)throw fail('unit already returned by another transaction');continue}this.db.prepare('INSERT INTO inventory_releases VALUES(?,?,?)').run(key,unit,receipt);count++}
   this.db.prepare('UPDATE inventory_slots SET available=available+? WHERE slot=?').run(count,r.slot);
   this.db.prepare("UPDATE inventory_leases SET released=released+?,status=CASE WHEN released+?=quantity THEN 'released' ELSE status END WHERE key=?").run(count,count,key);
   this.event('released',key,{receipt,units,count});return count;
  });
 }
 expire(key,now,{bookingAbsent=false}={}){const r=this.get(key);if(!r||r.status!=='held'||r.expires>now||bookingAbsent!==true||r.booking)throw fail('expiry requires known absent booking and no uncertain effect');return this.release(key,Array.from({length:r.quantity},(_,i)=>i+1),'expired:'+key,{known:true})}
 byBooking(owner,booking){return this.db.prepare('SELECT * FROM inventory_leases WHERE owner=? AND booking=?').get(owner,booking)}
 active(owner){return this.db.prepare("SELECT * FROM inventory_leases WHERE owner=? AND status!='released'").all(owner)}
 snapshot(){return {slots:this.db.prepare('SELECT * FROM inventory_slots ORDER BY slot').all(),leases:this.db.prepare('SELECT * FROM inventory_leases ORDER BY key').all(),events:this.db.prepare('SELECT * FROM inventory_events ORDER BY id').all()}}
 close(){this.db.close()}
}

export function sharedGateway({inventory,api,db,inner}){
 const bindings=new Map();
 async function context(request){const session=await api.rvSession(request,db);if(!session)throw fail('session required',401);const {document}=await api.rvRead(db,session.workspace_id);return {session,document}}
 async function bindWorkspace(client,slot){
  const request=new Request('https://local-qa.invalid/review/api/state',{headers:{cookie:client.cookie}}),{session,document}=await context(request);
  const q=api.rvSelectedCatalog(document).order.quote,res=inventory.contract.resource;
  if(q.productId!==res.product_id||q.offer.date!==res.departure||q.offer.room!==res.room||!inventory.contract.slots[slot])throw fail('source product/departure/room contract mismatch');
  bindings.set(session.workspace_id,{slot,generation:client.generation});return session.workspace_id;
 }
 async function sync(owner,doc){
  for(const lease of inventory.active(owner)){
   const b=doc.state.bookings.find(b=>b.id===lease.booking);if(!b)continue;
   const pending=doc.meta.receipts.some(r=>r.kind==='travel'&&r.status==='accepted_pending_query'&&doc.state.invoices.some(i=>i.id===r.invoiceId&&i.bookingId===b.id));
   if(pending){inventory.uncertain(lease.key);continue}
   inventory.bind(lease.key,b.id,b.status==='confirmed'?'confirmed':'held');
   for(const refund of doc.state.refunds.filter(r=>r.bookingId===b.id&&r.status==='refunded'))inventory.release(lease.key,refund.roomIds,refund.id,{known:true});
   if(b.status==='expired')inventory.release(lease.key,Array.from({length:b.rooms},(_,i)=>i+1),'booking-expired:'+b.id,{known:true});
  }
 }
 async function handle(request){
  const path=new URL(request.url).pathname;
  try{
   if(path==='/review/qa-fixtures/v1/seed'){
    const {session}=await context(request);if(inventory.active(session.workspace_id).length)throw fail('active or uncertain shared inventory must be reconciled before reseed');
   }
   if(path!=='/review/api/command')return inner.handle(request);
   const {session,document}=await context(request),binding=bindings.get(session.workspace_id),body=await request.clone().json();
   if(!binding)return inner.handle(request);
   if(request.headers.get('x-hope-qa-generation')!==String(binding.generation))throw fail('stale shared fixture generation');
   let lease=null;
   if(body.action==='book'&&session.account.role==='member'){
    if(!/^[a-f0-9-]{36}$/.test(body.requestId))throw fail('request key required',400);
    const key=session.workspace_id+':'+binding.generation+':'+body.requestId;
    const q=api.rvSelectedCatalog(document).order.quote,res=inventory.contract.resource;
    if(q.productId!==res.product_id||q.offer.date!==res.departure||q.offer.room!==res.room)throw fail('selected resource changed');
    lease=inventory.reserve({key,slot:binding.slot,owner:session.workspace_id,generation:binding.generation,fingerprint:JSON.stringify(body),quantity:q.selection.rooms,now:Date.now()});
    if(lease.status==='uncertain')throw fail('query existing uncertain booking before retry');
    inventory.uncertain(key); // Fence before crossing into another store. A crash cannot free this hold.
   }
   let response;
   try{response=await inner.handle(request)}catch{throw fail('effect uncertain; query before replay',503)}
   const next=(await api.rvRead(db,session.workspace_id)).document;
   if(lease){
    const recorded=next.meta.requests.find(r=>r.id===body.requestId),booking=recorded?.result?.id;
    if(booking&&next.state.bookings.some(b=>b.id===booking))inventory.bind(lease.key,booking,'held');
    else if(response.status>=400&&response.status<500){inventory.bind(lease.key,'known-rejected:'+body.requestId,'held');inventory.release(lease.key,Array.from({length:lease.quantity},(_,i)=>i+1),'known-rejected:'+body.requestId,{known:true})}
    // 5xx or an unrecorded 2xx leaves uncertain allocation; no compensation/retry guess.
   }
   await sync(session.workspace_id,next);return response;
  }catch(e){return Response.json({error:e.status?e.message:'shared fixture effect uncertain',scope:'shared-test-inventory'}, {status:e.status||503})}
 }
 return {handle,bindWorkspace,sync};
}
