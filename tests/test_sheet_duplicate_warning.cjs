const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../sheets/DuplicateOrderWarning.gs'), 'utf8');
const day = hour => new Date(Date.UTC(2026, 10, 14, hour));
const format = (date, timeZone) =>
  new Intl.DateTimeFormat('en-CA', {timeZone, year: 'numeric', month: '2-digit', day: '2-digit'}).format(date);

function run(orders, {row = 6, col = 17, endRow = row, endCol = col, title = '액자화환', header = '보내는분 요약'} = {}) {
  const toasts = [];
  const headers = Array(16).fill('');
  headers[0] = '배송일';
  headers[4] = '설치장소';
  headers[15] = header;
  const sheet = {
    getName: () => title,
    getLastRow: () => 4 + orders.length,
    getRange: (from, column, length) => ({
      getDisplayValues: () => [headers],
      getValues: () => Array.from({length}, (_, i) => [orders[from - 5 + i]?.[column] ?? '']),
    }),
  };
  const fixture = {
    range: {getSheet: () => sheet, getRow: () => row, getLastRow: () => endRow,
      getColumn: () => col, getLastColumn: () => endCol},
    source: {getSpreadsheetTimeZone: () => 'Asia/Seoul', toast: (...params) => toasts.push(params)},
  };
  vm.runInNewContext(source + '\nwarnDuplicateOrderOnEdit(fixture);', {
    fixture, Date, Utilities: {formatDate: format},
  });
  return toasts;
}

test('warns on the second order using calendar day and DB summary even when raw sender differs', () => {
  const first = {2: day(1), 6: '가상 장례식장', 16: '가상시 제5대 대표 김가상', 17: '김가상'};
  assert.deepEqual(run([first], {row: 5}), []);
  const second = {2: day(9), 6: '가상   장례식장', 16: '가상협회 김가상', 17: '김가상'};
  const alerts = run([first, second]);
  assert.equal(alerts.length, 1);
  assert.match(alerts[0][0], /5행·6행/);
  assert.doesNotMatch(alerts[0][0], /김가상|가상 장례식장/);
});

test('waits for all three fields and keeps different dates, venues and summaries separate', () => {
  const first = {2: day(2), 6: '가상 장례식장', 17: '김가상'};
  for (const mismatch of [{2: day(20)}, {6: '다른 장례식장'}, {17: '박가상'}, {17: ''}, {17: '#REF!'}, {2: ''}]) {
    assert.deepEqual(run([first, {...first, ...mismatch}]), []);
  }
});

test('checks pasted orders once, skips unrelated edits and handles a changed schema', () => {
  const a = {2: day(2), 6: '가상 장례식장', 17: '김가상'};
  const b = {2: day(2), 6: '가상 예식장', 17: '박가상'};
  const fixture = [a, b, a, b, {...a, 17: '최가상'}];
  const alerts = run(fixture, {row: 7, endRow: 9, col: 2, endCol: 17});
  assert.equal(alerts.length, 1);
  assert.match(alerts[0][0], /5행·7행, 6행·8행/);
  assert.deepEqual(run(fixture, {row: 9}), []);
  assert.deepEqual(run(fixture, {row: 7, col: 16}), []);
  assert.deepEqual(run(fixture, {row: 7, title: '다른 탭'}), []);
  assert.deepEqual(run(fixture, {row: 7, header: '다른 제목'}), []);
});
