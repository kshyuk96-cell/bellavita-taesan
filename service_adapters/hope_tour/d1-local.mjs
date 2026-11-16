// A real SQLite-backed D1 transport; only review/fixture tables may be accessed.
import {DatabaseSync} from 'node:sqlite';
export class LocalD1 {
  constructor(){
    this.sql=new DatabaseSync(':memory:');this.sql.exec('PRAGMA max_page_count=8192');
    this.fixtureIsolationCertificate=Object.freeze({environment:'isolated-local',network:'none',operationalTables:false});
    this.sql.exec(`CREATE TABLE review_limits(id TEXT PRIMARY KEY,count INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE review_sessions(token_hash TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,account TEXT NOT NULL,created TEXT NOT NULL,expires TEXT NOT NULL);
      CREATE TABLE review_workspaces(id TEXT PRIMARY KEY,revision INTEGER NOT NULL DEFAULT 0,payload TEXT NOT NULL,created TEXT NOT NULL,expires TEXT NOT NULL);
      CREATE TABLE qa_fixture_runs(run_id TEXT PRIMARY KEY,workspace TEXT UNIQUE,generation INTEGER,dataset TEXT,source TEXT);
      CREATE TABLE qa_fixture_events(id INTEGER PRIMARY KEY,run_id TEXT,generation INTEGER,kind TEXT,receipt TEXT);
      CREATE TABLE qa_fixture_cas(ok INTEGER NOT NULL CHECK(ok=1));
      CREATE TABLE qa_fixture_snapshots(run_id TEXT,generation INTEGER,payload TEXT,PRIMARY KEY(run_id,generation));`);
    this.statements=0;
  }
  prepare(sql){
    if(typeof sql!=='string'||sql.length>6000||/;|--|\/\*|\b(?:ATTACH|DETACH|PRAGMA|DROP|ALTER|CREATE|VACUUM)\b/i.test(sql))throw Error('SQL boundary rejected');
    const tables=[...sql.matchAll(/\b(?:FROM|INTO|UPDATE(?!\s+SET\b)|JOIN)\s+([a-z_]+)/gi)].map(x=>x[1]);
    const allowed=new Set(['review_limits','review_sessions','review_workspaces','qa_fixture_runs','qa_fixture_events','qa_fixture_snapshots','qa_fixture_cas']);
    if(!tables.length||tables.some(t=>!allowed.has(t)))throw Error('operational table rejected');
    if(++this.statements>10000)throw Error('SQL execution limit');
    const db=this;
    return {bind(...values){return db.bound(sql,values)},...this.bound(sql,[])};
  }
  bound(sql,values){
    const db=this;return {first(){return db.sql.prepare(sql).get(...values)||null},
      all(){return {results:db.sql.prepare(sql).all(...values)}},
      run(){const r=db.sql.prepare(sql).run(...values);return {meta:{changes:Number(r.changes)}}}};
  }
  batch(statements){
    this.sql.exec('BEGIN IMMEDIATE');try{const rows=statements.map(s=>s.run());this.sql.exec('COMMIT');return rows}
    catch(e){this.sql.exec('ROLLBACK');throw e}
  }
  close(){this.sql.close()}
}
