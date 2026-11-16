// Opt-in local-only bridge. Business calculations are delegated to pinned service functions.
export const PROTOCOL='hope-actual-fixtures/v1';
export const DEFINITIONS={
 U01:{grade:'hope',months:0},U02:{grade:'happy',months:0},U03:{grade:'premium',months:0},
 U04:{grade:'hope',months:12,DP:600000,BP:150000,SP:100000},
 U05:{grade:'hope',months:1,DP:0,BP:0,SP:0,historic:75000,historicCategory:'culture'},
 U06:{grade:'hope',months:20,DP:1000000,BP:150000,SP:100000},
 U07:{grade:'hope',months:20,DP:999999,BP:100001,SP:0,historic:1},
 U08:{grade:'hope',months:1,DP:50000,BP:25000,SP:0,arrears:6},
 U09:{grade:'hope',months:24,DP:750000,BP:85000,SP:0,historic:450000},
 U10:{grade:'hope',months:1,DP:50000,BP:25000,SP:0}};
export const PRODUCTS=[
 {id:'P01',title:'TEST 국내',category:'domestic',target:300000,stock:10},
 {id:'P02',title:'TEST 해외',category:'overseas',target:1000000,stock:1},
 {id:'P03',title:'TEST 문화',category:'culture',target:100000,stock:0},
 {id:'P04',title:'TEST 크루즈',category:'overseas',target:1000000,stock:3,rooms:['inside','balcony']}];
const capsByGrade={hope:{domestic:1500,overseas:1000,culture:5000},happy:{domestic:2000,overseas:1500,culture:7000},premium:{domestic:2500,overseas:1800,culture:10000}};
const clone=v=>structuredClone(v),now=doc=>new Date(doc.meta.day+'T01:00:00Z').toISOString();
const keyEqual=(a,b)=>typeof a==='string'&&typeof b==='string'&&a.length===64&&b.length===64&&[...a].reduce((x,c,i)=>x|(c.charCodeAt(0)^b.charCodeAt(i)),0)===0;
const error=(message,status=409)=>Object.assign(Error(message),{status});
export function createFixtureBridge({api,db,appFetch,env,sourceRevision,enabled=false,setupKey}){
 if(enabled!==true||db.fixtureIsolationCertificate?.environment!=='isolated-local'||db.fixtureIsolationCertificate?.operationalTables!==false||!/^[a-f0-9]{64}$/.test(setupKey))throw error('dedicated fixture binding required',403);
 const sql=(q,...args)=>db.prepare(q).bind(...args),requireKey=request=>{if(!keyEqual(setupKey,request.headers.get('x-hope-qa-key')))throw error('setup authentication required',403)};
 const own=doc=>doc.state.contracts.find(c=>doc.state.bindings.some(b=>b.accountId===c.accountId&&b.userId==='review-member'&&b.state!=='revoked'));
 const apply=(doc,command,actor=api.rvAccounts[1],context={})=>{
  const result=api.coApply(doc.state,{...command,day:doc.meta.day,acknowledgeSandbox:true},{id:actor.actorId,role:actor.role},now(doc),context);
  doc.state=result.state;return result.result;
 };
 const run=async(doc,action,fields={},actor=api.rvAccounts[0])=>{
  const out=await api.rvApply(doc,{action,...fields,requestId:crypto.randomUUID()},actor,env);
  Object.assign(doc,out.document);return out.result;
 };
 function select(doc,product,departureDays=120){
  const ct=own(doc),rate={hope:300,happy:500,premium:800}[ct.grade];
  const normal=Number((BigInt(product.target)*10000n+BigInt(10000-rate)-1n)/BigInt(10000-rate));
  const selected=api.rvPrepareProduct({id:product.id,title:product.title,status:'published',price:normal},doc.meta.day);
  const departure=new Date(new Date(doc.meta.day+'T00:00:00Z').getTime()+departureDays*86400000).toISOString().slice(0,10);
  selected.product.terms.departures=[departure];selected.config.offers[0].date=departure;selected.config.offers[0].stock=product.stock;
  if(product.rooms){selected.product.terms.rooms=product.rooms;selected.config.offers=[...product.rooms.map((r,i)=>({...selected.config.offers[0],id:'qa-'+r,room:r,stock:i?1:2,adultPrice:i?normal+200000:normal}))]}
  selected.order.quote=api.quoteOrder(selected.product,selected.config,{offerId:selected.config.offers[0].id,adults:1,children:0,rooms:1,extras:[]},new Date(now(doc)));
  doc.meta.selectedCatalog=selected;
  const gradeBpCapBps=Object.fromEntries(Object.keys(capsByGrade).map(g=>[g,capsByGrade[g][product.category]]));
  apply(doc,{action:'travelPolicy',policy:{label:'QA customer-example policy, not operational approval',allocationVersion:'principal-first-test-v1',productId:product.id,bpCapBps:1000,gradeBpCapBps,cancelTiers:[{days:0,bps:0}]}});
  return api.ciQuote(doc.state,ct.id,selected.order,doc.meta.day);
 }
 async function historicalUse(doc,total,category='overseas'){
  select(doc,{id:'qa-historic',title:'TEST 완료 이력',target:total,stock:1,category},1);
  const booked=await run(doc,'book',{expectedTotal:total});
  for(const inv of doc.state.invoices.filter(i=>i.bookingId===booked.id&&i.status==='unpaid'))await run(doc,'payInvoice',{invoiceId:inv.id});
  const cipher=await api.ciSeal([{name:'TEST traveller',nationality:'KR',documents:'not_required'}],env.REVIEW_DATA_KEY,'qa:'+booked.id);
  apply(doc,{action:'integrationTravelers',bookingId:booked.id,documentsChecked:true},api.rvAccounts[1],{travelersCipher:cipher,travelersCount:1});
  apply(doc,{action:'integrationNotice',bookingId:booked.id,message:'TEST only',meeting:'TEST only'});
  await run(doc,'advanceDay',{days:1},api.rvAccounts[1]);apply(doc,{action:'integrationAdvance',bookingId:booked.id});
  await run(doc,'advanceDay',{days:1},api.rvAccounts[1]);apply(doc,{action:'integrationAdvance',bookingId:booked.id});
 }
 async function makeDataset(id){
  const definition=DEFINITIONS[id];if(!definition)throw error('unknown fixed dataset',400);
  const end='2026-11-16',start=definition.months?api.rvNewDocument(end).meta.day:end;
  let doc=api.rvNewDocument(start);
  if(definition.months){
   const d=new Date(end+'T00:00:00Z');d.setUTCMonth(d.getUTCMonth()-(definition.months-1));
   doc=api.rvNewDocument(d.toISOString().slice(0,10));
   await run(doc,'registerCard',{scenario:'success'});
   await run(doc,'signup',{grade:definition.grade,consents:{terms:true,privacy:true,recurring:true,marketing:false}});
   for(let i=1;i<definition.months;i++)await run(doc,'runCycle',{},api.rvAccounts[1]);
   const ct=own(doc);let balances=api.clubBalances(doc.state,ct.id);
   // Only bonus/SP are granted. Refundable DP always has actual paid invoice provenance.
   const desiredBP=(definition.BP||0)+(id==='U05'?25000:0);
   for(const [book,target] of [['BP',desiredBP],['SP',definition.SP||0]]){
    const delta=target-balances[book];if(delta<0)throw error('initial benefit exceeds specified dataset');
    if(delta)apply(doc,{action:'grant',contractId:ct.id,book,amount:delta,sourceKey:'qa-'+id+'-'+book,reason:'TEST fixture, no refundable principal'});
   }
   doc.meta.initialDay=doc.meta.day;
   if(definition.historic)await historicalUse(doc,definition.historic,definition.historicCategory);
   if(definition.arrears){await run(doc,'registerCard',{scenario:'decline'});for(let i=0;i<=definition.arrears;i++)await run(doc,'runCycle',{},api.rvAccounts[1])}
   if(!definition.arrears)select(doc,PRODUCTS[1]);
   balances=api.clubBalances(doc.state,ct.id);
   for(const book of ['DP','BP','SP'])if(balances[book]!==definition[book])throw error('prepared balance differs from contract: '+book);
  }
  doc.meta.dataset={id,version:PROTOCOL,sourceRevision,exampleOnly:true,customerApproved:false,operationalDataChanged:false};
  return doc;
 }
 async function sessionFor(request){const session=await api.rvSession(request,db);if(!session)throw error('actual review authentication required',401);return session}
 async function register(request,body){
  requireKey(request);const session=await sessionFor(request);const {document,row}=await api.rvRead(db,session.workspace_id);
  if(row.revision!==0||document.state.contracts.length||document.state.accounts.length)throw error('existing populated review workspace must not be rebound');
  if(!/^[a-f0-9-]{36}$/.test(body.runId)||body.sourceRevision!==sourceRevision)throw error('run/source binding mismatch');
  sql('INSERT INTO qa_fixture_runs(run_id,workspace,generation,dataset,source) VALUES(?,?,0,NULL,?)',body.runId,row.id,sourceRevision).run();
  return {runId:body.runId,generation:0,sourceRevision};
 }
 async function seed(request,body){
  requireKey(request);const session=await sessionFor(request),runRow=sql('SELECT * FROM qa_fixture_runs WHERE run_id=? AND workspace=?',body.runId,session.workspace_id).first();
  if(!runRow)throw error('fixture run scope denied',403);
  if(!Number.isInteger(body.generation)||body.generation!==runRow.generation||body.sourceRevision!==sourceRevision)throw error('stale source/generation');
  const {document,row}=await api.rvRead(db,session.workspace_id);
  if(document.state.attempts.some(a=>a.status==='pending')||document.meta.receipts.some(r=>r.status==='accepted_pending_query'))throw error('uncertain effects must be reconciled before reset');
  const prepared=await makeDataset(body.dataset),snapshot={balances:api.rvView(prepared,api.rvAccounts[0]).balances,principal:own(prepared)?api.clubPrincipalState(prepared.state,own(prepared).id,now(prepared)):null};
  const generation=runRow.generation+1;if(generation>100)throw error('generation limit');
  const receipt={runId:body.runId,generation,dataset:body.dataset,sourceRevision,initialDigest:await api.rvHash(JSON.stringify(snapshot)),snapshot,operationalDataChanged:false,realCharges:false};
  const result=db.batch([
   db.prepare('INSERT INTO qa_fixture_snapshots(run_id,generation,payload) VALUES(?,?,?)').bind(body.runId,runRow.generation,row.payload),
   db.prepare('UPDATE review_workspaces SET payload=?,revision=revision+1 WHERE id=? AND revision=?').bind(JSON.stringify(prepared),row.id,row.revision),
   db.prepare('INSERT INTO qa_fixture_cas(ok) SELECT CASE WHEN changes()=1 THEN 1 ELSE 0 END'),
   db.prepare('UPDATE qa_fixture_runs SET generation=?,dataset=? WHERE run_id=? AND generation=?').bind(generation,body.dataset,body.runId,runRow.generation),
   db.prepare('INSERT INTO qa_fixture_cas(ok) SELECT CASE WHEN changes()=1 THEN 1 ELSE 0 END'),
   db.prepare('INSERT INTO qa_fixture_events(run_id,generation,kind,receipt) VALUES(?,?,?,?)').bind(body.runId,generation,'seed',JSON.stringify(receipt))]);
  if(result[1].meta.changes!==1||result[3].meta.changes!==1)throw error('fixture CAS failed');
  return receipt;
 }
 async function handle(request){
  const url=new URL(request.url),path=url.pathname;
  try{
   if(path.startsWith('/review/qa-fixtures/v1/')){
    if(request.method!=='POST')throw error('method denied',405);
    if(request.headers.get('origin')&&request.headers.get('origin')!==url.origin)throw error('origin denied',403);
    const raw=await request.text();if(raw.length>4096)throw error('fixture request limit',413);const body=JSON.parse(raw);
    const fields=path.endsWith('/register')?['runId','sourceRevision']:['runId','generation','dataset','sourceRevision'];
    if(!body||typeof body!=='object'||Object.keys(body).length!==fields.length||Object.keys(body).some(k=>!fields.includes(k)))throw error('fixed setup fields required',400);
    const result=path.endsWith('/register')?await register(request,body):path.endsWith('/seed')?await seed(request,body):(()=>{throw error('fixture path missing',404)})();
    return Response.json(result);
   }
   if(path==='/review/api/command'||path==='/review/form/command'){
    const session=await sessionFor(request),managed=sql('SELECT * FROM qa_fixture_runs WHERE workspace=?',session.workspace_id).first();
    if(managed){
     if(request.headers.get('x-hope-qa-generation')!==String(managed.generation))throw error('stale fixture generation');
     const type=request.headers.get('content-type')||'',raw=await request.clone().text();
     const action=type.includes('application/json')?JSON.parse(raw).action:new URLSearchParams(raw).get('action');
     if(['resetWorkspace','seedScenario'].includes(action))throw error('use the source-fenced setup adapter');
    }
   }
   return await appFetch(request);
  }catch(e){return Response.json({error:e.status?e.message:'fixture request failed',scope:'isolated-local'}, {status:e.status||400})}
 }
 return {handle,makeDataset,select,protocol:PROTOCOL};
}
