const fs = require('node:fs');
const {routePlan} = require('./route-core');

const {orders, workers, points, nearGap, delay, start, manualTimes} = JSON.parse(fs.readFileSync(0, 'utf8'));
const distances = new Map();
function distance(from, to) {
  if (from === to) return 0;
  const key = JSON.stringify([from, to]);
  if (distances.has(key)) return distances.get(key);
  if (!Object.hasOwn(points, from) || !Object.hasOwn(points, to)) return null;
  const [a, b] = points[from].map(x => x * Math.PI / 180);
  const [c, d] = points[to].map(x => x * Math.PI / 180);
  const h = Math.sin((c - a) / 2) ** 2 + Math.cos(a) * Math.cos(c) * Math.sin((d - b) / 2) ** 2;
  const km = 12742 * Math.asin(Math.sqrt(Math.min(1, h)));
  distances.set(key, km);
  return km;
}
function leg(_day, from, to, at, target) {
  const km = distance(from, to);
  if (km === null) return {error: '주소 좌표 확인 필요'};
  const minutes = from === to ? 0 : Math.ceil(15 + km * 1.3 * 3) + delay;
  const depart = Number.isFinite(target) ? Math.max(at, target - minutes) : at;
  return {mode: 'APPROX', depart, arrive: depart + minutes};
}
leg.minGap = order => {
  if (/천안시|아산시/.test(order.address)) return 300;
  const km = distance(order.origin, order.address);
  return km !== null && km <= 10 ? nearGap : 240;
};

const plan = routePlan(orders, workers, {start, pickup: 10, install: 30, lead: 60, manualTimes}, leg);
const ids = visit => (visit.members || [visit]).map(order => order.order);
const routes = plan.days.flatMap(day => day.workers).filter(worker => worker.jobs.length).map(worker => {
  let carried = 0;
  let maxLoad = 0;
  const visits = worker.jobs.slice().reverse().map((visit, index) => {
    carried += visit.qty;
    maxLoad = Math.max(carried, maxLoad);
    const row = {orders: ids(visit), event: visit.event, address: visit.address, done: visit.done,
      quantity: visit.qty, carried, repickup: Boolean(visit.pickup && index < worker.jobs.length - 1)};
    if (visit.pickup) carried = 0;
    return row;
  }).reverse();
  return {name: worker.name, maxLoad, visits};
});
const missed = plan.unassigned.flatMap(visit => ids(visit).map(order => ({order,
  reason: visit.issue || (visit.reasons || []).find(reason => !reason.includes('→')) || '이동·설치 시간 또는 재픽업 간격 확인'})));
process.stdout.write(JSON.stringify({routes, missed}));
