const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../sheets/DuplicateOrderWarning.gs'), 'utf8');
const date = hour => new Date(Date.UTC(2026, 10, 14, hour));
const format = (value, timeZone, pattern) => {
  const options = pattern === 'HH:mm:ss'
    ? {timeZone, hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'}
    : {timeZone, year: 'numeric', month: '2-digit', day: '2-digit'};
  return new Intl.DateTimeFormat('en-CA', options).format(value);
};
const resolved = (venueId = 'site-a', floor = '3', hall = 'alpha') =>
  ({status: 'resolved', venueId, floor, hall, evidence: 'fixture-registry-v1'});
const order = (place, extra = {}) => ({2: date(1), 3: .5, 6: place, 17: '모의발신자', ...extra});

function run(orders, mappings = {}, options = {}) {
  const {row = 6, endRow = row, col = 17, endCol = col, title = '액자화환', header = '보내는분 요약'} = options;
  const toasts = [], resolutions = [];
  const headers = Array(16).fill('');
  headers[0] = '배송일'; headers[1] = options.clockHeader || '행사시간';
  headers[4] = '설치장소'; headers[15] = header;
  const sheet = {getName: () => title, getLastRow: () => orders.length + 4,
    getRange: (from, column, length) => ({getDisplayValues: () => [headers],
      getValues: () => Array.from({length}, (_, i) => [orders[from - 5 + i]?.[column] ?? ''])})};
  const fixture = {range: {getSheet: () => sheet, getRow: () => row, getLastRow: () => endRow,
    getColumn: () => col, getLastColumn: () => endCol},
    source: {getSpreadsheetTimeZone: () => 'Asia/Seoul', toast: (...args) => toasts.push(args)}};
  const resolver = input => {resolutions.push(input); if (options.outage) throw Error('fixture outage'); return mappings[input.place];};
  const context = {Date, fixture, resolver, Utilities: {formatDate: format}};
  vm.runInNewContext(source + '\nwarnDuplicateOrderOnEdit(fixture, resolver);', context);
  return {toasts, resolutions, context};
}

test('trusted road/jibun and reversed-token mapping match the same actual hall', () => {
  const a = '서울 가상로 10 예시관 3층 알파홀';
  const b = '알파홀 예시관 가상동 100 서울';
  const result = run([order(a), order(b, {2: date(9), 3: '오후 12:00'})], {[a]: resolved(), [b]: resolved()});
  assert.equal(result.toasts.length, 1);
  assert.match(result.toasts[0][0], /중복 주문 의심: 5행·6행/);
  assert.doesNotMatch(result.toasts[0][0], /서울|알파|모의발신자/);
  assert.equal(result.resolutions.length, 2);
});

test('same building alone needs review; different floors or halls are separate', () => {
  const rows = [order('가상관'), order('가상관')];
  for (const value of [resolved('site-a', '', ''), resolved('site-a', '3', '')]) {
    const {toasts} = run(rows, {'가상관': value});
    assert.match(toasts[0][0], /장소 확인 필요/);
    assert.doesNotMatch(toasts[0][0], /중복 주문 의심/);
  }
  for (const other of [resolved('site-a', '4', 'alpha'), resolved('site-a', '3', 'beta'), resolved('site-b')]) {
    assert.deepEqual(run([order('첫표기'), order('다른표기')], {'첫표기': resolved(), '다른표기': other}).toasts, []);
  }
});

test('ambiguous mapping and resolver outage never become confident duplicate warnings', () => {
  const result = run([order('첫표기'), order('다른표기')], {'첫표기': resolved(),
    '다른표기': {status: 'ambiguous', candidateVenueIds: ['site-a', 'site-b']}});
  assert.match(result.toasts[0][0], /장소 확인 필요/);
  assert.doesNotMatch(result.toasts[0][0], /중복 주문 의심/);
  const uncertainFloor = run([order('첫표기'), order('다른표기')], {'첫표기': resolved(),
    '다른표기': {status: 'ambiguous', floor: '4', hall: 'beta', candidateVenueIds: ['site-a']}});
  assert.match(uncertainFloor.toasts[0][0], /장소 확인 필요/);
  const same = run([order('가상관'), order('가상관')], {}, {outage: true});
  assert.match(same.toasts[0][0], /장소 확인 필요/);
  const different = run([order('첫표기'), order('다른표기')], {}, {outage: true});
  assert.equal(different.toasts.length, 1);
  assert.match(different.toasts[0][0], /장소 조회 미연결\/실패로 일부 대조 미완료 \(2행\)/);
  assert.doesNotMatch(different.toasts[0][0], /중복 주문 의심|장소 확인 필요|5행·6행/);
  assert.match(run([order('가상관'), order('가상관')]).toasts[0][0], /장소 확인 필요/);
});

test('verified main/annex wings separate rooms; one-sided wing requires review', () => {
  const rows = [order('첫표기'), order('다른표기')];
  const main = {...resolved(), wing: '본관'};
  const annex = {...resolved(), wing: '별관'};
  assert.deepEqual(run(rows, {'첫표기': main, '다른표기': annex}).toasts, []);
  const oneSide = run(rows, {'첫표기': main, '다른표기': resolved()});
  assert.match(oneSide.toasts[0][0], /장소 확인 필요: 5행·6행/);
  assert.doesNotMatch(oneSide.toasts[0][0], /중복 주문 의심/);
  assert.match(run(rows, {'첫표기': main, '다른표기': main}).toasts[0][0], /중복 주문 의심/);
  assert.match(run(rows, {'첫표기': resolved(), '다른표기': resolved()}).toasts[0][0], /중복 주문 의심/);
});

test('missing resolver exposes incomplete comparison without inventing duplicate pairs', () => {
  const {context} = run([order('가상관')], {}, {row: 5});
  const rows = ['첫표기', '다른표기', '셋째표기'].map((place, index) => ({row: index + 5,
    day: '2026-11-14', clock: '12:00:00', place, sender: '모의', edited: true}));
  const result = context.duplicateOrderFind_(rows, undefined, 1000);
  assert.equal(result.unresolved, 3);
  assert.equal(result.duplicates.length, 0);
  assert.equal(result.review.length, 0);
  assert.equal(result.incomplete, false);
  const alone = context.duplicateOrderFind_(rows.slice(0, 1), undefined, 1000);
  assert.equal(alone.unresolved, 0);
});

test('date, exact event time, and DB sender summary must all agree', () => {
  const base = order('가상관');
  for (const mismatch of [{2: date(20)}, {3: '12:01'}, {3: '12:00:01'}, {3: ''},
    {17: '다른발신자'}, {17: '#REF!'}, {2: ''}, {6: ''}]) {
    assert.deepEqual(run([base, {...base, ...mismatch}], {'가상관': resolved()}).toasts, []);
  }
  assert.equal(run([base, {...base, 16: '직함이 다른 원문'}], {'가상관': resolved()}).toasts.length, 1);
});

test('paste produces one toast, ignores unrelated edits, and validates all relevant headers', () => {
  const rows = [order('가상관'), order('가상관'), order('가상관')];
  const result = run(rows, {'가상관': resolved()}, {row: 6, endRow: 7, col: 2, endCol: 17});
  assert.equal(result.toasts.length, 1);
  assert.match(result.toasts[0][0], /5행·6행/);
  for (const options of [{col: 16}, {header: '다른 제목'}, {clockHeader: '배송시각'}, {title: '다른 탭'}]) {
    assert.deepEqual(run(rows, {'가상관': resolved()}, options).toasts, []);
  }
});

test('trusted IDs need evidence, do not treat exact text as sufficient, and bound comparisons', () => {
  const noEvidence = {...resolved(), evidence: ''};
  assert.match(run([order('가상관'), order('가상관')], {'가상관': noEvidence}).toasts[0][0], /장소 확인 필요/);
  const {context} = run([order('가상관')], {}, {row: 5});
  const rows = Array.from({length: 5}, (_, i) => ({row: i + 5, day: '2026-11-14', clock: '12:00:00',
    place: '가상관', sender: '모의', edited: true}));
  const result = context.duplicateOrderFind_(rows, () => resolved(), 3);
  assert.equal(result.duplicates.length, 3);
  assert.equal(result.incomplete, true);
});

test('large edits are explicitly incomplete and helper has no write, geocode or trigger calls', () => {
  const rows = Array.from({length: 102}, () => order('가상관'));
  const {toasts, resolutions} = run(rows, {'가상관': resolved()}, {row: 5, endRow: 106});
  assert.match(toasts[0][0], /확인 범위를 넘었어요/);
  assert.equal(resolutions.length, 0);
  assert.doesNotMatch(source, /\.setValues?\(|UrlFetchApp|Maps\.|ScriptApp|function\s+onEdit\s*\(/);
});
