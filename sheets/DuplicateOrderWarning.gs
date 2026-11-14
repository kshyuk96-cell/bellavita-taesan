/**
 * Call from the existing spreadsheet onEdit(e) handler, including for pastes.
 * This check reads the edited ledger and only shows a toast.
 */
function warnDuplicateOrderOnEdit(e) {
  if (!e || !e.range || !e.source) return;
  const edit = e.range;
  const sheet = edit.getSheet();
  const firstOrderRow = 5;
  const dateColumn = 2;
  const placeColumn = 6;
  const summaryColumn = 17;
  const first = Math.max(firstOrderRow, edit.getRow());
  const lastOrderRow = sheet.getLastRow();
  const last = Math.min(lastOrderRow, edit.getLastRow());
  const relevant = [dateColumn, placeColumn, summaryColumn].some(
    column => edit.getColumn() <= column && column <= edit.getLastColumn()
  );
  if (sheet.getName() !== '액자화환' || first > last || !relevant) return;

  const headers = sheet.getRange(4, dateColumn, 1, summaryColumn - dateColumn + 1).getDisplayValues()[0];
  if (headers[0] !== '배송일' || headers[4] !== '설치장소' ||
      String(headers[15] || '').replace(/\s/g, '') !== '보내는분요약') return;

  const zone = e.source.getSpreadsheetTimeZone();
  const dates = sheet.getRange(firstOrderRow, dateColumn, lastOrderRow - firstOrderRow + 1, 1)
    .getValues().map(([value]) => value instanceof Date && !isNaN(value) ? value : null);
  const dateKey = date => Utilities.formatDate(date, zone, 'yyyy-MM-dd');
  const editedDates = dates.slice(first - firstOrderRow, last - firstOrderRow + 1).filter(Boolean);
  const wanted = new Set(editedDates.map(dateKey));
  if (!wanted.size) return;

  const timestamps = editedDates.map(date => date.getTime());
  // Bound expensive timezone formatting while allowing for long days around clock changes.
  const margin = 26 * 60 * 60 * 1000;
  const lower = Math.min(...timestamps) - margin;
  const upper = Math.max(...timestamps) + margin;
  const candidates = [];
  dates.forEach((date, i) => {
    if (!date || date < lower || date > upper) return;
    const day = dateKey(date);
    if (wanted.has(day)) candidates.push({i, day});
  });
  const start = candidates[0].i;
  const span = candidates[candidates.length - 1].i - start + 1;
  const places = sheet.getRange(start + firstOrderRow, placeColumn, span, 1).getValues();
  const summaries = sheet.getRange(start + firstOrderRow, summaryColumn, span, 1).getValues();
  const groups = new Map();
  for (const {i, day} of candidates) {
    const place = duplicateOrderText_(places[i - start][0]);
    const sender = duplicateOrderText_(summaries[i - start][0]);
    if (!place || !sender) continue;
    const key = JSON.stringify([day, place, sender]);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(i + firstOrderRow);
  }

  const matches = Array.from(groups.values()).filter(
    rows => rows.length > 1 && rows.some(row => first <= row && row <= last)
  );
  if (!matches.length) return;
  const preview = matches.slice(0, 3).map(rows => (
    rows.slice(0, 6).map(row => row + '행').join('·') +
    (rows.length > 6 ? ' 외 ' + (rows.length - 6) + '건' : '')
  )).join(', ');
  const remaining = matches.length > 3 ? ' 외 ' + (matches.length - 3) + '묶음' : '';
  e.source.toast(
    '배송일·설치장소·보내는 분 요약이 같은 주문이 있어요. 원장 ' +
      preview + remaining + '을 출고 전에 확인해 주세요.',
    '중복 주문 의심',
    10
  );
}

function duplicateOrderText_(value) {
  if (typeof value !== 'string') return '';
  const text = value.normalize('NFKC').trim().replace(/\s+/g, ' ').toLowerCase();
  return /^#(n\/a|ref!|value!|div\/0!|num!|name\?|null!|error!|spill!)$/.test(text) ? '' : text;
}
