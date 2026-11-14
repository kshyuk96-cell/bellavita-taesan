const ROUTE_DEFAULT_CAPACITY = 2;
const ROUTE_MAX_CAPACITY = 6;
const ROUTE_INSTALL_LEAD = 60;

const ROUTE_WEEKEND_ROSTER = {
  '김석헌': {preferred:['서울 북부','의정부','강동','청량리']},
  '조홍재': {daysOff:[0],preferred:['강서','인천','부천','김포','일산']},
  '권태헌': {daysOff:[0],avoid:['신도림']},
  '박세순': {avoid:['군포','의왕','안양']},
  '김경훈': {daysOff:[0],preferred:['부천','인천']},
  '우영진': {},
  '정원준': {capacity:6,preferAfter:720,bulk:true},
  '성주익': {daysOff:[0]}
};

function routeWorkerRules(name,rules) {
  const base=Object.hasOwn(ROUTE_WEEKEND_ROSTER,name)?ROUTE_WEEKEND_ROSTER[name]:{};
  return {...base,...rules,daysOff:[...new Set([...(base.daysOff||[]),...(rules.daysOff||[])])]};
}

function routeTiming(order,policy) {
  const install=order.installMinutes ?? policy.install;
  const early=order.wedding===true && order.event<660;
  const earliest=order.accessStart ?? (early?540:0);
  const deadline=order.installBy ?? (early?order.event-1:order.event-policy.lead);
  const target=early?earliest:Math.min(order.event-policy.lead-policy.install,deadline-install);
  return {install,earliest,deadline,target};
}

/** All times are integer minutes after midnight in Korea. */
function routeSolve(orders, workers, policy, leg) {
  const active = workers.filter(w => w.enabled);
  const workerRules=active.map(w=>routeWorkerRules(w.name,w.rules||{}));
  const ids = new Set();
  for (const o of orders) {
    if (!o.id || ids.has(o.id)) throw Error('주문 식별자가 비어 있거나 중복됩니다.');
    ids.add(o.id);
  }
  const seed = () => ({jobs: [], load: 0, count: 0, center: '', score: 0, combinedScore: 0, bulkCapacityScore: 0});
  const days = [...new Set(orders.map(o => o.day))].sort();
  const result = {days: [], unassigned: []};
  for (const day of days) {
    const weekday=new Date(day+'T00:00:00Z').getUTCDay();
    const jobs = orders.filter(o => o.day === day).sort((a,b) => a.event-b.event || a.id.localeCompare(b.id));
    let states = [{routes: active.map(seed), missed: []}];
    for (const o of jobs) {
      const next = [];
      for (const state of states) {
        const failures = new Set();
        if (o.issue) failures.add(o.issue);
        if (!active.length) failures.add('사용 가능한 배송원 없음');
        if (!o.issue) active.forEach((w, i) => {
          const r = state.routes[i];
          const p = r.jobs[r.jobs.length - 1];
          const rule = workerRules[i];
          if (rule.daysOff?.includes(weekday) ||
              (rule.centers?.length && !rule.centers.includes(o.center)) ||
              (o.driver && o.driver !== w.name)) {
            failures.add('배송원 제약 또는 기존 배송원 지정');
            return;
          }
          for (const pickup of p ? [false,true] : [true]) {
            const reject = reason => failures.add(reason);
            const load = (pickup ? 0 : r.load) + o.qty;
            if (!Number.isInteger(w.capacity) || w.capacity > ROUTE_MAX_CAPACITY || load > w.capacity) {
              reject('1회 운반 한도 초과'); continue;
            }
            if (!pickup && o.center !== r.center) continue;
            const gap = p ? o.event - p.event : Infinity;
            const minGap = leg.minGap ? Math.max(leg.minGap(o), p ? leg.minGap(p) : 0) : (policy.gap ?? 240);
            const exception = pickup && gap < minGap;
            if (exception && !(!leg.minGap && gap >= 150 && pickup && r.count % 2 === 0)) {
              reject('주문 ' + p.order + ' → ' + o.order + ' 행사 간격 ' + gap + '분 (기준 ' + minGap + '분)'); continue;
            }
            const legs = [];
            const priorPickup = pickup && !p && rule.priorPickup === true;
            const pickupMinutes = pickup && !priorPickup ? policy.pickup : 0;
            const firstStart = priorPickup ? (rule.priorDeparture ?? policy.start) : policy.start;
            let at = p ? p.done : Math.max(firstStart, rule.start ?? 0);
            const move = (from, to, deadline) => {
              let l;
              const verifiedMinutes = policy.manualTimes?.[o.id];
              if (!p && from === o.origin && to === o.address &&
                  Number.isInteger(verifiedMinutes) && verifiedMinutes > 0) {
                const depart = Number.isFinite(deadline)
                  ? Math.max(at, deadline - verifiedMinutes)
                  : at;
                l = {mode:'APPROX', depart, arrive:depart + verifiedMinutes, lines:[],
                  source:'운영자 확인 이동시간 ' + verifiedMinutes + '분'};
              } else {
                l = leg(day, from, to, at, deadline);
              }
              if (!l || !Number.isInteger(l.depart) || !Number.isInteger(l.arrive) ||
                  l.depart < at || l.arrive < l.depart ||
                  !['TRANSIT','WALK','APPROX','SIMULATION'].includes(l.mode)) {
                reject(from + ' → ' + to + ' 경로 확인 실패' + (l?.error ? ' (' + l.error + ')' : ''));
                return null;
              }
              const row = {...l, from, to};
              legs.push(row); at = l.arrive;
              return row;
            };
            let pickupAt = null;
            if (pickup) {
              if (p) {
                const back = move(p.address, o.origin, null);
                if (!back) continue;
                if (exception && back.arrive - p.done > 30) {
                  reject('주문 ' + p.order + ' → ' + o.center + ' 복귀 30분 초과'); continue;
                }
              }
              pickupAt = at;
              at += pickupMinutes;
            }
            const timing=routeTiming(o,policy);
            const deadline=Math.min(timing.deadline,rule.end ?? 1440);
            const arrivalTarget=Math.min(timing.target,deadline-timing.install);
            if (!move(pickup ? o.origin : p.address,o.address,arrivalTarget)) continue;
            if (pickup && legs.length===1) pickupAt=priorPickup ? null : legs[0].depart-pickupMinutes;
            const arrival=at;
            const installStart=Math.max(arrival,timing.earliest);
            const done=installStart+timing.install;
            if (arrival>arrivalTarget || done>deadline) {
              const late = [];
              if (arrival > arrivalTarget) late.push('도착 목표 ' + (arrival - arrivalTarget) + '분 초과');
              if (done > deadline) late.push('설치 마감 ' + (done - deadline) + '분 초과');
              reject(late.join(' / ')); continue;
            }
            const reasons=[];
            let penalty=0, regionPenalty=0;
            if(rule.avoid?.some(v=>o.address.includes(v))){
              reasons.push('제외 요청 지역 · 전체 주문 처리와 가능한 동선 기준으로 선택');
              regionPenalty+=10000;
            }
            if(rule.preferred?.length && !rule.preferred.some(v=>o.address.includes(v))){
              reasons.push('선호지역 외 · 가능한 일정과 동선 기준으로 선택');
              regionPenalty+=60;
            }
            if(rule.preferAfter && o.event<rule.preferAfter) penalty+=30;
            if(o.qty>=3 && !rule.bulk) penalty+=200;
            const combined=o.members?.length>1;
            const combinedScore=r.combinedScore+(combined ? o.qty*o.qty : 0);
            const bulkCapacityScore=r.bulkCapacityScore+(combined && o.qty>=3 ? o.qty*w.capacity : 0);
            const travel = legs.reduce((n,l) => n+l.arrive-l.depart,0);
            const idle = p ? Math.max(0,done-p.done-travel-timing.install-(pickup ? policy.pickup : 0)) : 0;
            const step = {...o, pickup, pickupAt, pickupMinutes, priorPickup,
              batch: pickup ? o.id : p.batch, count: pickup ? 1 : r.count+1,
              legs, arrival, arrivalTarget, installStart, done, deadline, slack: deadline-done, exception, reasons};
            const routes = state.routes.slice();
            routes[i] = {jobs: [...r.jobs,step], load, count:step.count, center:o.center, combinedScore, bulkCapacityScore,
              penalty:(r.penalty||0)+penalty, regionPenalty:(r.regionPenalty||0)+regionPenalty, score:r.score+travel+Math.ceil(idle/4)+timing.install+o.qty*10+pickupMinutes+Math.max(0,30-step.slack)};
            next.push({routes, missed:state.missed});
          }
        });
        next.push({routes:state.routes, missed:[...state.missed,{...o,reasons:[...failures].slice(0,16)}]});
      }
      for(const state of next){
        const rank=[0,0,0,0,0,0];
        for(const route of state.routes){
          rank[0]-=route.combinedScore;
          rank[1]-=route.bulkCapacityScore;
          rank[3]+=route.penalty||0;
          rank[4]+=route.score*route.score;
          rank[5]+=route.regionPenalty||0;
        }
        rank[2]=state.missed.reduce((n,o)=>n+(o.members?.length||1),0);
        state.rank=rank;
      }
      next.sort((a,b)=>a.rank[0]-b.rank[0] || a.rank[1]-b.rank[1] || a.rank[2]-b.rank[2] ||
        a.rank[3]-b.rank[3] || a.rank[4]-b.rank[4] || a.rank[5]-b.rank[5]);
      const seen=new Set();
      const limit=policy.beam||24;
      states=[];
      for(const state of next){
        const key=JSON.stringify([state.missed.map(m=>m.id),state.routes.map(r=>[r.jobs[r.jobs.length-1]?.id,r.jobs[r.jobs.length-1]?.done,r.load,r.count,r.score,r.penalty,r.regionPenalty,r.combinedScore,r.bulkCapacityScore])]);
        if(seen.has(key)) continue;
        seen.add(key);
        states.push(state);
        if(states.length>=limit) break;
      }
    }
    const best = states[0];
    result.days.push({day, workers:active.map((w,i)=>({name:w.name, ...best.routes[i]}))});
    result.unassigned.push(...best.missed);
  }
  return result;
}

function routeEventPlace(address) {
  return String(address??'').normalize('NFC').trim()
    .replace(/(서울)특별시(?=\s|$)/g,'$1')
    .replace(/(인천|부산|대구|광주|대전|울산)광역시(?=\s|$)/g,'$1')
    .replace(/\s+/g,' ')
    .replace(/\s*([()])\s*/g,'$1');
}

function routeBulkVisits(orders,policy,workers,maxBatch=ROUTE_MAX_CAPACITY) {
  const groups=new Map();
  const grouped=new Map();
  const active=workers.filter(w=>w.enabled && Number.isInteger(w.capacity) && w.capacity<=ROUTE_MAX_CAPACITY);
  const bulkExtra=policy.bulkExtra??policy.install;
  const addVisit=members=>{
    const qty=members.reduce((n,o)=>n+o.qty,0);
    if(qty<2 || (qty===2 && members.length<2)) return;
    const first=members[0];
    grouped.set(first.id,{...first,order:members.map(o=>o.order).join(', '),qty,
      installMinutes:Math.max(policy.install,...members.map(o=>o.installMinutes||0))+(qty>=3 ? bulkExtra : 0),
      members:members.map(o=>({id:o.id,order:o.order}))});
    members.slice(1).forEach(o=>grouped.set(o.id,null));
  };
  for(const o of orders){
    if(o.issue) continue;
    const key=JSON.stringify([o.day,o.center,o.origin,routeEventPlace(o.address),o.event,o.wedding,o.accessStart,o.installBy,o.driver||'']);
    const group=groups.get(key)||[];
    group.push(o);groups.set(key,group);
  }
  for(const members of groups.values()){
    const first=members[0];
    const weekday=new Date(first.day+'T00:00:00Z').getUTCDay();
    const capacity=active.reduce((max,w)=>{
      const rule=routeWorkerRules(w.name,w.rules||{});
      if((first.driver && first.driver!==w.name) || rule.daysOff.includes(weekday) ||
         (rule.centers?.length && !rule.centers.includes(first.center))) return max;
      return Math.max(max,w.capacity);
    },0);
    const batches=[];
    for(const order of members){
      const batch=batches.find(b=>b.qty+order.qty<=Math.min(capacity,maxBatch));
      if(batch){
        batch.members.push(order);
        batch.qty+=order.qty;
      }else{
        batches.push({members:[order],qty:order.qty});
      }
    }
    for(const batch of batches) addVisit(batch.members);
  }
  return orders.map(o=>grouped.has(o.id)?grouped.get(o.id):o).filter(Boolean);
}

function routePlan(orders, workers, policy, leg) {
  let visits=routeBulkVisits(orders,policy,workers);
  let result=routeSolve(visits,workers,policy,leg);
  const missedGroups=result.unassigned.filter(o=>o.members?.length>1);
  if(missedGroups.length){
    const originals=new Map(orders.map(o=>[o.id,o]));
    const regroup=new Map(missedGroups.map(o=>[o.id,o.members.map(m=>originals.get(m.id))]));
    const missedCount=plan=>plan.unassigned.reduce((n,o)=>n+(o.members?.length||1),0);
    const combinedScore=plan=>plan.days.reduce((sum,day)=>sum+day.workers.reduce((n,w)=>n+w.combinedScore,0),0);
    const assignedGroups=new Map();
    for(const day of result.days) for(const worker of day.workers) for(const job of worker.jobs){
      if(job.members?.length>1) assignedGroups.set(job.id,worker.name);
    }
    const initialVisits=visits;
    const largest=Math.max(...missedGroups.map(o=>o.qty));
    for(let limit=largest-1;limit>=1;limit--){
      const split=initialVisits.flatMap(o=>{
        if(regroup.has(o.id)) return routeBulkVisits(regroup.get(o.id),policy,workers,limit);
        const driver=assignedGroups.get(o.id);
        return driver ? {...o,driver} : o;
      });
      const retry=routeSolve(split,workers,policy,leg);
      const retained=retry.days.reduce((count,day)=>count+day.workers.reduce((n,w)=>
        n+w.jobs.filter(j=>assignedGroups.get(j.id)===w.name).length,0),0);
      if(retained===assignedGroups.size && combinedScore(retry)>=combinedScore(result) &&
         missedCount(retry)<missedCount(result)){
        visits=split;
        result=retry;
        if(!result.unassigned.length) break;
      }
    }
  }
  result.alternatives = [];
  if (result.unassigned.some(o=>!o.issue)) {
    for (const gap of leg.minGap ? [] : [210,180,150]) {
      const plan = routeSolve(visits,workers,{...policy,gap},leg);
      result.alternatives.push({gap, unassigned:plan.unassigned.flatMap(o=>(o.members||[o]).map(m=>m.order)),
        recovered:result.unassigned.flatMap(o=>o.members||[o]).filter(o=>!plan.unassigned.flatMap(m=>m.members||[m]).some(m=>m.id===o.id)).map(o=>o.order)});
    }
    result.staffReview = true;
  }
  return result;
}

function routeRules(note) {
  const rules = {}, pending = [];
  const weekdays = '일월화수목금토';
  for (const raw of String(note || '').split(/[\n;,]/)) {
    const line = raw.trim();
    if (!line) continue;
    const off = line.match(/^([일월화수목금토])요일 (?:배송 )?불가$/);
    const hour = line.match(/^(\d{1,2}):(\d{2}) (이후 출근|이전 종료)$/);
    const center = line.match(/^(구로센터|동대문센터|수원센터)만$/);
    if (off) {
      if (!rules.daysOff) rules.daysOff = [];
      rules.daysOff.push(weekdays.indexOf(off[1]));
    }
    else if (hour && +hour[1]<24 && +hour[2]<60) {
      const t = +hour[1]*60 + +hour[2];
      if (hour[3]==='이후 출근') rules.start=Math.max(rules.start ?? 0,t);
      else rules.end=Math.min(rules.end ?? 1440,t);
    } else if (center) {
      if (rules.centers && !rules.centers.includes(center[1])) pending.push(line);
      else rules.centers = [center[1]];
    } else pending.push(line);
  }
  return {rules,pending};
}

function routeChanges(draft,current) {
  if(draft.sheetId!==current.sheetId) throw Error('시트 식별자 변경');
  if(draft.workers.some(w=>w.pending.length))
    throw Error('특별지침 확인 필요 항목을 처리하세요.');
  const indexed=new Map(current.orders.map(o=>[o.id,o]));
  const changes=[];
  for (const day of draft.plan.days) for (const w of day.workers) for (const job of w.jobs) {
    if(job.legs.some(l=>!['TRANSIT','WALK','APPROX'].includes(l.mode))) throw Error('실제 대중교통 검증 필요');
    for(const member of job.members||[job]){
      if(draft.writeIds && !draft.writeIds.includes(member.id)) continue;
      const now=indexed.get(member.id);
      if(!now || now.issue || !now.feeRow) throw Error(member.order+' 주문 또는 배송비DB 확인 필요');
      if(now.anchor!==w.name || now.visible!==w.name) changes.push({row:now.row,feeRow:now.feeRow,driver:w.name});
    }
  }
  if (draft.version!==current.version) throw Error('저장 이후 주문 또는 설정이 변경되었습니다. 다시 계산하세요.');
  return changes;
}

function routeNewOrderCandidates(savedDay,workers,order,policy,now,leg) {
  if(order.issue) return [{status:'검증 필요',reason:order.issue}];
  const result=[];
  const timing=routeTiming(order,policy);
  for(const route of savedDay.workers){
    const w=workers.find(x=>x.name===route.name);
    if(!w || !w.enabled || w.pending?.length || (order.driver && order.driver!==w.name)) continue;
    const rule=routeWorkerRules(w.name,w.rules||{});
    const weekday=new Date(order.day+'T00:00:00Z').getUTCDay();
    if(rule.daysOff.includes(weekday) || (rule.centers?.length && !rule.centers.includes(order.center))) continue;
    if(order.qty>w.capacity || w.capacity>ROUTE_MAX_CAPACITY) continue;
    for(let i=0;i<=route.jobs.length;i++){
      const previous=route.jobs[i-1],next=route.jobs[i];
      if(next && !next.pickup) continue;
      const gapWith = other => leg.minGap ? Math.max(leg.minGap(order),leg.minGap(other)) : 240;
      if(order.wedding && ((previous && order.event-previous.event<gapWith(previous)) || (next && next.event-order.event<gapWith(next)))) continue;
      const returnLeg=previous?.urgent ? previous.legs[previous.legs.length-1] : null;
      const previousDone=Math.max(previous?.done ?? policy.start,returnLeg?.arrive??0);
      const previousLocation=returnLeg?.to ?? previous?.address;
      const start=Math.max(now,previousDone,policy.start,rule.start??0);
      const nextLeg=next?.legs[0];
      const rejoinBy=next ? Math.min(nextLeg?.depart??0,next.pickupAt??Infinity) : Infinity;
      if(start>=rejoinBy) continue;
      const legs=[];
      let at=start;
      const move=(from,to,target)=>{
        const l=leg(order.day,from,to,at,target);
        if(!l || !['TRANSIT','WALK','APPROX','SIMULATION'].includes(l.mode) ||
           !Number.isInteger(l.depart)||!Number.isInteger(l.arrive)||l.depart<at||l.arrive<l.depart) return false;
        legs.push({...l,from,to});at=l.arrive;return true;
      };
      if(previous && !move(previousLocation,order.origin,null)){
        result.push({name:w.name,status:'검증 필요',reason:'지정 출고지까지 경로 확인 필요'});continue;
      }
      const pickupAt=at;
      at+=policy.pickup;
      const deadline=Math.min(timing.deadline,rule.end??1440);
      // Urgent recommendations use the earliest available transit, with a hard arrival cutoff.
      const arrivalTarget=Math.min(timing.target,deadline-timing.install);
      if(!move(order.origin,order.address,null)){
        result.push({name:w.name,status:'검증 필요',reason:'출고지에서 신규 배송지까지 경로 확인 필요'});continue;
      }
      const arrival=at, installStart=Math.max(arrival,timing.earliest),done=installStart+timing.install;
      if(arrival>arrivalTarget || done>deadline) continue;
      at=done;
      if(next && (!nextLeg || !move(order.address,nextLeg.from,rejoinBy) || at>rejoinBy)) continue;
      const elapsed=done-start;
      const total=done-now;
      const status=next?'일정 사이 가능':total<=120?'배송 후 가능 · 2시간 이내':'배송 후 가능 · 2시간 초과';
      const soft=rule.avoid?.some(v=>order.address.includes(v))?10000:rule.preferred?.length&&!rule.preferred.some(v=>order.address.includes(v))?20:0;
      const job={...order,pickup:true,pickupAt,pickupMinutes:policy.pickup,batch:order.id,count:1,
        arrival,arrivalTarget,installStart,done,deadline,slack:deadline-done,legs,urgent:true,
        reasons:soft>=10000?['제외 요청 지역 · 긴급 주문 검토']:[]};
      result.push({name:w.name,index:i,status,elapsed,total,done,job,rank:next?0:total<=120?1:2,regionPenalty:soft});
    }
  }
  const best=new Map();
  for(const c of result.sort((a,b)=>(a.rank??Infinity)-(b.rank??Infinity) || (a.total??Infinity)-(b.total??Infinity) || (a.regionPenalty??0)-(b.regionPenalty??0))) if(!best.has(c.name)) best.set(c.name,c);
  return [...best.values()];
}

function routeOrderNumbers(input) {
  const values=String(input??'').trim().split(/[\s,;]+/).filter(Boolean);
  if(!values.length || values.some(v=>!/^\d+$/.test(v))) throw Error('주문번호를 쉼표, 공백 또는 줄바꿈으로 입력하세요.');
  return [...new Set(values)];
}

function routeAddNewOrders(draft,current,now,leg,input) {
  if(draft.sheetId!==current.sheetId || draft.day!==current.day) throw Error('저장본과 주문 조회 대상이 다릅니다.');
  const savedDay=draft.plan.days.find(d=>d.day===current.day);
  if(!savedDay) throw Error('기존 배송원 일정이 있는 저장본을 선택하세요.');
  const requested=routeOrderNumbers(input);
  const known=new Set(savedDay.workers.flatMap(w=>w.jobs.flatMap(j=>(j.members||[j]).map(o=>o.id))));
  const latest=new Map(current.orders.map(o=>[o.id,o]));
  const working={...savedDay,workers:savedDay.workers.map(w=>({...w,jobs:w.jobs.slice()}))};
  const eligible=new Map(current.workers.filter(w=>w.enabled).map(w=>[w.name,w]));
  const workers=draft.workers.filter(w=>eligible.has(w.name)).map(w=>{
    const currentWorker=eligible.get(w.name);
    if(!currentWorker.dbRules) return w;
    const {rules,pending}=routeRules([currentWorker.dbNote,w.note].filter(Boolean).join('\n'));
    const db=currentWorker.dbRules;
    return {...w,pending,rules:{...rules,...db,
      start:Math.max(db.start ?? 0,rules.start ?? 0),
      end:Math.min(db.end ?? 1440,rules.end ?? 1440)}};
  });
  for(const route of working.workers) for(const job of route.jobs){
    for(const m of job.members||[job]){
      const live=latest.get(m.id), old=draft.orders.find(o=>o.id===m.id);
      if(job.done>=now && (!live || !old || live.issue ||
        ['day','address','event','center','origin','qty'].some(key=>live[key]!==old[key]) ||
        (live.driver && live.driver!==route.name))) throw Error(m.order+' 기존 주문 변경 · 저장본을 먼저 확인하세요.');
    }
  }
  const suggestions=[],writeIds=[];
  const byNumber=new Map(current.orders.map(o=>[String(o.order),o]));
  const newOrders=[];
  for(const number of requested){
    const order=byNumber.get(number);
    const reason=!order?'선택일의 대상 주문에서 찾을 수 없음':
      order.visible?'배송관리에 배송원이 이미 입력됨':
      order.anchor?'배송비DB에 배송원이 이미 지정됨':
      known.has(order.id)?'기존 저장본에 이미 일정이 있음':'';
    if(reason) suggestions.push({order:number,center:order?.center||'',candidates:[],skip:reason});
    else newOrders.push(order);
  }
  const retry=new Set(newOrders.map(o=>o.id));
  const unresolved=draft.plan.unassigned.flatMap(visit=>{
    const members=(visit.members||[visit]).filter(member=>!retry.has(member.id));
    if(!members.length) return [];
    if(members.length===(visit.members?.length||1)) return [visit];
    return members.map(member=>{
      const order=latest.get(member.id)??draft.orders.find(o=>o.id===member.id);
      return {...visit,...member,...order,members:undefined,qty:order?.qty,reasons:visit.reasons};
    });
  });
  newOrders.sort((a,b)=>a.event-b.event);
  for(const order of newOrders){
    const candidates=routeNewOrderCandidates(working,workers,order,current.policy,now,leg);
    const best=candidates.find(c=>c.job);
    suggestions.push({order:order.order,center:order.center,candidates:candidates.map(({job,index,rank,regionPenalty,...c})=>({...c,pickupAt:job?.pickupAt,arrival:job?.arrival,deadline:job?.deadline}))});
    if(!best){unresolved.push({...order,reasons:[order.issue||'기존 일정을 유지하며 도착할 수 있는 경로 없음']});continue;}
    working.workers.find(w=>w.name===best.name).jobs.splice(best.index,0,best.job);
    writeIds.push(order.id);
  }
  return {...current,workers,writeIds,suggestions,
    plan:{days:[working],unassigned:unresolved,alternatives:[]}};
}

module.exports = {routePlan};
