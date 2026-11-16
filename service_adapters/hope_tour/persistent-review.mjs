import {DatabaseSync} from 'node:sqlite';
import {LocalD1} from './d1-local.mjs';
export class PersistentReview extends LocalD1 {
 constructor(config,inventory){
  super();this.sql.close();this.sql=new DatabaseSync('/business/review.sqlite');this.sql.exec('PRAGMA busy_timeout=5000; PRAGMA max_page_count=8192');
  const meta=this.sql.prepare('SELECT * FROM qa_review_runtime WHERE id=1').get();
  if(meta?.contract_digest!==config.business_digest||meta.source!==config.source_revision||meta.task!==config.business_contract.task)throw Error('business contract/source/task mismatch');
  this.config=config;this.inventory=inventory;this.barrierUsed=false;
 }
 async verifyKey(key,api){
  const row=this.sql.prepare('SELECT key_check FROM qa_review_runtime WHERE id=1').get(),aad='qa-review:'+this.config.source_revision+':'+this.config.business_contract.task;
  const value={source:this.config.source_revision,task:this.config.business_contract.task,testOnly:true};
  if(!row.key_check){
   if(this.config.role!=='setup'||this.sql.prepare('SELECT count(*) AS n FROM review_workspaces').get().n!==0)throw Error('business key not provisioned');
   const sealed=await api.ciSeal(value,key,aad);this.sql.prepare('UPDATE qa_review_runtime SET key_check=? WHERE id=1 AND key_check IS NULL').run(JSON.stringify(sealed));
  }else{
   const opened=await api.ciOpen(JSON.parse(row.key_check),key,aad);if(JSON.stringify(opened)!==JSON.stringify(value))throw Error('business key context mismatch');
  }
  this.keyVerified=true;
 }
 bound(sql,values){
  const original=super.bound(sql,values),db=this;
  if(this.config?.role.startsWith('race-')&&/^UPDATE review_workspaces SET payload=/i.test(sql))return {...original,async run(){
   if(!db.barrierUsed){db.barrierUsed=true;const bound=db.inventory.db.prepare("SELECT aux FROM recovery_bindings WHERE label='shared-worker'").get();const aux=JSON.parse(bound.aux),doc=JSON.parse(values[0]);
    if(!doc.meta.requests.some(r=>r.id===aux.pay_request_id))throw Error('CAS barrier must observe the payment request');
    db.inventory.db.prepare('INSERT INTO recovery_barrier(role,request_id,revision) VALUES(?,?,?)').run(db.config.role,aux.pay_request_id,values[2]);
    const until=performance.now()+15000;
    while(db.inventory.db.prepare('SELECT released FROM recovery_gate WHERE id=1').get().released!==1){if(performance.now()>until)throw Error('business CAS barrier timeout');await new Promise(r=>setTimeout(r,20))}
    const out=original.run();db.inventory.db.prepare('UPDATE recovery_barrier SET changes=? WHERE role=?').run(out.meta.changes,db.config.role);return out;
   }return original.run();
  }};
  return original;
 }
}
