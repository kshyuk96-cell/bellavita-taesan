/** Read-only warning helper. Call AFTER existing onEdit has recalculated Q.
 * resolveVenue({row, place}) must use a preloaded, trusted venue registry.
 * Never geocode, write cells, or install triggers from this helper.
 */
function warnDuplicateOrderOnEdit(e, resolveVenue) {
  if (!e || !e.range || !e.source) return;
  const edit = e.range;
  const sheet = edit.getSheet();
  const first = Math.max(5, edit.getRow());
  const lastRow = sheet.getLastRow();
  const last = Math.min(lastRow, edit.getLastRow());
  if (sheet.getName() !== '액자화환' || first > last ||
      ![2, 3, 6, 17].some(c => edit.getColumn() <= c && c <= edit.getLastColumn())) return;
  const h = sheet.getRange(4, 2, 1, 16).getDisplayValues()[0];
  if (h[0] !== '배송일' || h[1] !== '행사시간' || h[4] !== '설치장소' ||
      String(h[15] || '').replace(/\s/g, '') !== '보내는분요약') return;
  if (last - first + 1 > 100 || lastRow - 4 > 50000) {
    e.source.toast('확인 범위를 넘었어요. 대상 주문을 나눠 중복 조회해 주세요.', '중복 확인 미완료', 10);
    return;
  }
  const zone = e.source.getSpreadsheetTimeZone();
  const dates = sheet.getRange(5, 2, lastRow - 4, 1).getValues();
  const day = value => value instanceof Date && !isNaN(value) ?
    Utilities.formatDate(value, zone, 'yyyy-MM-dd') : '';
  const editedDates = dates.slice(first - 5, last - 4).map(r => r[0])
    .filter(value => value instanceof Date && !isNaN(value));
  const wanted = new Set(editedDates.map(day));
  if (!wanted.size) return;
  const timestamps = editedDates.map(value => value.getTime());
  const margin = 26 * 60 * 60 * 1000;
  const lower = Math.min(...timestamps) - margin;
  const upper = Math.max(...timestamps) + margin;
  const candidates = [];
  for (let i = 0; i < dates.length; i++) {
    const value = dates[i][0];
    if (!(value instanceof Date) || isNaN(value) || value < lower || value > upper) continue;
    const date = day(value);
    if (wanted.has(date)) candidates.push({row: i + 5, day: date});
    if (candidates.length > 1000) break;
  }
  if (candidates.length > 1000) {
    e.source.toast('같은 날짜의 확인 대상이 많아요. 대상 주문을 나눠 조회해 주세요.', '중복 확인 미완료', 10);
    return;
  }
  const start = candidates[0].row;
  const length = candidates[candidates.length - 1].row - start + 1;
  const clocks = sheet.getRange(start, 3, length, 1).getValues();
  const places = sheet.getRange(start, 6, length, 1).getValues();
  const senders = sheet.getRange(start, 17, length, 1).getValues();
  const rows = candidates.map(r => ({row: r.row, day: r.day,
    clock: duplicateOrderClock_(clocks[r.row - start][0], zone),
    place: duplicateOrderText_(places[r.row - start][0]),
    sender: duplicateOrderText_(senders[r.row - start][0]),
    edited: first <= r.row && r.row <= last}));
  const result = duplicateOrderFind_(rows, resolveVenue, 1000);
  const preview = pairs => pairs.slice(0, 3).map(p => p[0] + '행·' + p[1] + '행').join(', ') +
    (pairs.length > 3 ? ' 외 ' + (pairs.length - 3) + '쌍' : '');
  const lines = [];
  if (result.duplicates.length) lines.push('중복 주문 의심: ' + preview(result.duplicates));
  if (result.review.length) lines.push('장소 확인 필요: ' + preview(result.review));
  if (result.unresolved) lines.push('장소 조회 미연결/실패로 일부 대조 미완료 (' + result.unresolved + '행)');
  if (result.incomplete) lines.push('확인 범위를 넘겨 일부 주문은 확인하지 못했어요.');
  if (lines.length) e.source.toast(lines.join('\n') + '\n원장에서 출고 전 확인해 주세요.', '주문 중복 점검', 10);
}

function duplicateOrderText_(value) {
  if (typeof value !== 'string' || value.length > 1000) return '';
  const text = value.normalize('NFKC').trim().replace(/\s+/g, ' ').toLowerCase();
  return /^#(?:n\/a|ref!|value!|div\/0!|num!|name\?|null!|error!|spill!)$/.test(text) ? '' : text;
}

function duplicateOrderClock_(value, zone) {
  if (value instanceof Date && !isNaN(value)) return Utilities.formatDate(value, zone, 'HH:mm:ss');
  if (typeof value === 'number' && Number.isFinite(value) && value >= 0 && value < 1) {
    const seconds = Math.round(value * 86400);
    if (seconds >= 86400) return '';
    return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
      .map(n => String(n).padStart(2, '0')).join(':');
  }
  const text = duplicateOrderText_(value);
  const match = /^(?:(오전|오후)\s*)?(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec(text);
  if (!match) return '';
  let hour = Number(match[2]);
  if (Number(match[3]) > 59 || Number(match[4] || 0) > 59 || hour > 23) return '';
  if (match[1]) {
    if (hour < 1 || hour > 12) return '';
    hour = hour % 12 + (match[1] === '오후' ? 12 : 0);
  }
  return [hour, Number(match[3]), Number(match[4] || 0)].map(n => String(n).padStart(2, '0')).join(':');
}

function duplicateOrderFind_(rows, resolveVenue, maxPairs) {
  const groups = new Map();
  const resolved = new Map();
  const seen = new Set();
  const result = {duplicates: [], review: [], unresolved: 0, incomplete: false};
  const resolve = row => {
    if (!resolved.has(row.row)) {
      let value = null;
      try { value = typeof resolveVenue === 'function' ? resolveVenue({row: row.row, place: row.place}) : null; }
      catch (_) { value = null; }
      value = value && typeof value === 'object' ? value : {};
      resolved.set(row.row, value);
      if (!trusted(value)) result.unresolved++;
    }
    return resolved.get(row.row);
  };
  const keyText = value => typeof value === 'string' && value.length <= 200 ? value.trim() : '';
  const trusted = value => value.status === 'resolved' && keyText(value.venueId) && keyText(value.evidence);
  const candidates = value => {
    const values = Array.isArray(value.candidateVenueIds) ? value.candidateVenueIds.slice(0, 10) : [];
    return new Set([trusted(value) ? keyText(value.venueId) : '', ...values.map(keyText)].filter(Boolean));
  };
  for (const row of rows) {
    if (!row.day || !row.clock || !row.place || !row.sender) continue;
    const key = JSON.stringify([row.day, row.clock, row.sender]);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  let count = 0;
  for (const group of groups.values()) {
    for (const left of group.filter(r => r.edited)) {
      for (const right of group) {
        if (left.row === right.row) continue;
        const pair = [left.row, right.row].sort((a, b) => a - b);
        const pairKey = JSON.stringify(pair);
        if (seen.has(pairKey)) continue;
        if (count >= maxPairs) { result.incomplete = true; return result; }
        seen.add(pairKey); count++;
        const a = resolve(left), b = resolve(right);
        const floorA = duplicateOrderText_(a.floor), floorB = duplicateOrderText_(b.floor);
        const hallA = duplicateOrderText_(a.hall), hallB = duplicateOrderText_(b.hall);
        const wingA = duplicateOrderText_(a.wing), wingB = duplicateOrderText_(b.wing);
        if (trusted(a) && trusted(b) && keyText(a.venueId) !== keyText(b.venueId)) continue;
        if (trusted(a) && trusted(b) && ((floorA && floorB && floorA !== floorB) ||
            (hallA && hallB && hallA !== hallB) || (wingA && wingB && wingA !== wingB))) continue;
        if (trusted(a) && trusted(b) && keyText(a.venueId) === keyText(b.venueId) &&
            floorA && floorB && hallA && hallB && wingA === wingB) {
          result.duplicates.push(pair);
          continue;
        }
        const ids = candidates(a);
        const overlaps = Array.from(candidates(b)).some(id => ids.has(id));
        // No broad warning for every unresolved place sharing a day/time/sender.
        if (left.place === right.place || overlaps) result.review.push(pair);
      }
    }
  }
  return result;
}
