"""Public funeral notice using the association obituary's classic design.

Design sources: association-aftercare-platform/components/ObituaryView.tsx,
components/MemorialPortrait.tsx and app/globals.css. Only the chosen customer's
account is shown; the ordinary family list remains complete.
"""
from html import escape
import re
from urllib.parse import quote, urlsplit

LILY = '''<div class="memorial-art" role="img" aria-label="영정사진 대신 표시되는 백합 추모 디자인"><svg viewBox="0 0 180 220" fill="none" aria-hidden="true"><ellipse cx="90" cy="108" rx="66" ry="88" stroke="currentColor" stroke-width=".55" opacity=".45"/><path d="M90 177c-5-32-4-56 2-83M89 152c-25 2-38-9-43-25 17 1 32 8 43 25ZM90 141c25-6 39-20 40-36-20 6-32 18-40 36Z" stroke="currentColor" stroke-width="1.2"/><path d="M94 104c-26-4-38-20-34-43 17 6 31 20 34 43ZM94 104c21-10 29-29 20-49-14 12-21 28-20 49ZM94 104c-15-20-15-39 1-54 12 18 12 36-1 54ZM94 104c-22 8-38 4-45-11 16-6 31-3 45 11ZM94 104c23 4 38-2 42-18-17-2-31 4-42 18Z" fill="currentColor" fill-opacity=".045" stroke="currentColor" stroke-width="1.1"/><path d="m94 104-3-21m3 21 11-20m-11 20-16-15" stroke="currentColor" stroke-width=".8"/><circle cx="91" cy="81" r="2" fill="currentColor"/><circle cx="106" cy="82" r="2" fill="currentColor"/><circle cx="76" cy="88" r="2" fill="currentColor"/><path d="M66 183h48M74 189h32" stroke="currentColor" stroke-width=".6"/></svg><span>소중한 기억을 담아</span></div>'''


def _text(value):
    return str(value or '').strip()


def _url(value, relative=False):
    value = _text(value)
    if any(ord(c) < 32 for c in value) or '\\' in value:
        return ''
    try:
        parsed = urlsplit(value)
        if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password:
            return value
        if relative and value.startswith('/') and not value.startswith('//'):
            return value
    except ValueError:
        pass
    return ''


def _phone(value):
    value = _text(value)
    return re.sub(r'[^0-9+]', '', value) if re.fullmatch(r'\+?[0-9 ()\-]{7,30}', value) else ''


def _customer_name(value):
    # Courtesy words are not part of a mourner's actual name (김상주 remains).
    return re.sub(r'\s*상주님\s*$', '', _text(value)).strip()


def _copy_button(value, label):
    return '<button type="button" class="copy" data-copy="' + escape(_text(value), quote=True) + '">' + escape(label) + '</button>'


def _mourners(value, host):
    rows = []
    for line in (_text(value) or _customer_name(host)).splitlines():
        line = line.strip()
        if not line:
            continue
        parts = re.split(r'[:：]', line, maxsplit=1)
        relation, names = (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else ('', line)
        rows.append('<div class="mourner-line"><span>' + escape(relation) + '</span><strong>' + escape(names) + '</strong></div>')
    return ''.join(rows)


def page(renderer, v, price, order_url, sample, og, url, preview=False):
    """Render source family details and one explicitly selected public account."""
    from erp_memorial_invitation import clean_mourner_accounts

    def e(value):
        return escape(_text(value), quote=True)

    def detail(label, value, accent=False):
        return ('<dt>' + e(label) + '</dt><dd class="preserve-lines' + (' accent' if accent else '') + '">' + e(value) + '</dd>') if _text(value) else ''

    venue, room, address = (_text(v.get(key)) for key in ('venue', 'funeralRoom', 'address'))
    name = _text(v.get('deceasedName'))
    title = _text(v.get('name')) or ('故 ' + name + ' 부고')
    greeting = _text(v.get('greeting')) or '삼가 고인의 명복을 빕니다.'
    departure = _text(v.get('departure'))
    if not departure:
        try:
            _, date, clock = renderer.date_words(v)
            departure = ' '.join(x for x in (date, clock) if x)
        except (AttributeError, TypeError, ValueError, KeyError):
            departure = ' '.join(_text(v.get(k)) for k in ('date', 'time')).strip()
    portrait_url = _url(v.get('portrait'))
    portrait = ('<img class="portrait-image" src="' + e(portrait_url) + '" alt="고인 영정사진" style="object-position:50% 35%">') if portrait_url else LILY
    portrait = '<div class="memorial-portrait ' + ('with-photo' if portrait_url else 'without-photo') + '"><div class="portrait-frame">' + portrait + '</div></div>'
    description = '<small>' + e(v['deceasedDescription']) + '</small>' if _text(v.get('deceasedDescription')) else ''
    death = '<p>' + e(v['deathDate']) + '</p>' if _text(v.get('deathDate')) else ''
    mourners = _mourners(v.get('mourners'), v.get('host'))
    family = '<section class="obit-section"><h3>상주</h3>' + mourners + '</section>' if mourners else ''
    venue_detail = '<dt>빈소</dt><dd>' + e(venue) + ('<br><strong>' + e(room) + '</strong>' if room and room != venue else '') + '</dd>'
    funeral = venue_detail + detail('입실', v.get('entrance')) + detail('입관', v.get('admission')) + detail('발인', departure, True) + detail('장지', v.get('burial'))
    notice = _text(v.get('originalNotice'))
    if notice:
        funeral += detail('알림글', notice)
    directions = ''
    if address or venue:
        map_url = 'https://map.naver.com/p/search/' + quote(address or venue, safe='')
        phone = _phone(v.get('funeralPhone'))
        directions = '<section class="obit-section"><h3>오시는 길</h3><strong>' + e(venue) + '</strong><p class="muted preserve-lines">' + e(address) + '</p><div class="map-card"><span aria-hidden="true">⌖</span><p>' + e(address or venue) + '</p><a class="btn secondary" href="' + e(map_url) + '" target="_blank" rel="noopener noreferrer">지도에서 위치 보기 ↗</a></div>' + ('<a class="text-link" href="tel:' + e(phone) + '">장례식장 전화 ' + e(v['funeralPhone']) + '</a>' if phone else '') + '</section>'
    accounts = clean_mourner_accounts(v.get('mournerAccounts'), v.get('host'))
    account_html = []
    for row in accounts:
        account_html.append('<details class="account"><summary>' + e(' '.join(x for x in (_text(row.get('relation')), _customer_name(row.get('name'))) if x)) + '<span>계좌 확인 ＋</span></summary><p>' + e(row.get('bank')) + ' ' + e(row.get('account')) + '</p><div><span>예금주 ' + e(row.get('holder') or _customer_name(row.get('name'))) + '</span>' + _copy_button(row['account'], '계좌번호 복사') + '</div></details>')
    contact = _phone(v.get('contactPhone'))
    contact_html = '<a class="contact-pill" href="tel:' + e(contact) + '">' + e(_customer_name(v.get('host'))) + ' 연락하기</a>' if contact else ''
    account_section = '<section class="obit-section"><h3>마음 전하실 곳</h3><p class="muted">따뜻한 마음에 깊이 감사드립니다.</p>' + ''.join(account_html) + contact_html + '</section>' if account_html or contact_html else ''
    source_url = _url(v.get('sourceUrl'))
    source = '<p class="obit-notice"><a class="text-link" href="' + e(source_url) + '" target="_blank" rel="noopener noreferrer">원본 부고장 보기 ↗</a></p>' if source_url else ''
    photo = _url(sample, relative=True)
    product_photo = '<img src="' + e(photo) + '" alt="쌀빛그림화환 안내 배너">' if photo else ''
    target = _url(order_url, relative=True)
    action = '<a class="action" href="' + e(target) + '">쌀빛그림화환 주문하기 · ' + f'{int(price):,}' + '원</a>' if target and not preview else '<span class="action">주문 미리보기 · ' + f'{int(price):,}' + '원</span>'
    products = '<section class="products"><h2>쌀빛그림화환</h2><p>조문의 마음을 쌀빛그림화환으로 전해 주세요.</p>' + product_photo + action + '<p class="notice">발인 시각은 배송 시각이 아닙니다. 실제 배송 일시와 빈소는 주문 접수 후 확인합니다.</p></section>'
    public_url = _url(url)
    share = '<div class="obit-share">' + _copy_button(public_url, '부고 링크 복사') + '<span class="copy-status" role="status" aria-live="polite"></span></div>' if public_url else '<div class="obit-share"><span class="copy-status" role="status" aria-live="polite"></span></div>'
    body = ('<p class="preview-notice">미리보기 · 결제 연결 비활성</p>' if preview else '') + '<header class="obit-header"><div class="obit-home">' + e(venue or '부고 안내') + '</div><p class="eyebrow">IN LOVING MEMORY</p>' + portrait + '<h1>삼가 고인의 명복을 빕니다</h1></header><section class="deceased"><span>故</span><h2>' + e(name) + description + '</h2>' + death + '<p class="greeting">' + e(greeting) + '</p></section>' + family + '<section class="obit-section"><h3>장례 안내</h3><dl class="funeral-details">' + funeral + '</dl></section>' + directions + account_section + source + share + '<footer class="obit-footer">Bellavita<small>마음을 정성껏 전합니다</small></footer>' + products
    return '<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>' + e(title) + '</title><meta property="og:title" content="' + e(title) + '"><meta property="og:image" content="' + e(_url(og)) + '"><meta property="og:url" content="' + e(public_url) + '"><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;600;700&amp;family=Noto+Serif+KR:wght@400;500;600;700&amp;display=swap"><style>' + CSS + '</style></head><body><main class="public-page"><article class="obituary classic">' + body + '</article></main><script>document.addEventListener("click",async function(event){const button=event.target.closest("button[data-copy]");if(!button)return;const status=document.querySelector(".copy-status");try{await navigator.clipboard.writeText(button.dataset.copy);if(status)status.textContent="복사했습니다.";}catch(error){if(status)status.textContent="복사하지 못했습니다. 내용을 길게 눌러 복사해 주세요.";}});</script></body></html>'

CSS = """

/* Adapted from association-aftercare-platform/app/globals.css: obituary classic. */
:root{--accent:#28564b;--green:#28564b;--ink:#283c36;--paper:#f6f7f4}
*{box-sizing:border-box}body{margin:0;color:var(--ink);background:var(--paper);font-family:"Noto Sans KR",Arial,sans-serif;font-size:14px;-webkit-font-smoothing:antialiased}a{color:inherit;text-decoration:none}button{font:inherit;cursor:pointer}.public-page{padding:24px 12px}.muted{color:#85918b;line-height:1.8}.preserve-lines{white-space:pre-line;overflow-wrap:anywhere}.btn{border:1px solid transparent;border-radius:7px;padding:12px 18px;display:inline-flex;align-items:center;justify-content:center;gap:8px;line-height:1.5;font-size:13px;font-weight:500}.secondary{background:white;border-color:#dfe5df;color:#46584e}.text-link{display:inline-flex;gap:6px;align-items:center;color:var(--green);background:none;border:0;padding:0;font-size:12px;line-height:1.7}.copy{border:1px solid #dfe5df;border-radius:5px;background:white;color:#46584e;padding:7px 10px;font-size:11px}.preview-notice{text-align:center;padding:10px;margin:0;background:#fff2cd;font-size:12px}.products{padding:28px 32px;background:#f2f3ea;text-align:center}.products img{width:100%;height:auto}.products h2{font-size:18px}.products .action{display:block;padding:16px;background:#28564b;color:white;border-radius:7px;font-size:14px}.products .notice{font-size:11px;line-height:1.8;color:#85918b}.copy-status{display:block;min-height:20px;font-size:11px;color:#647d65}.deceased{padding-top:24px}.obituary {
  max-width: 480px;
  margin: 0 auto;
  background: #fffdfa;
  box-shadow: 0 5px 35px #293a2110;
  overflow: hidden;
  color: #42493f;
  overflow-wrap: anywhere;
}
.obit-header {
  text-align: center;
  padding: 35px 30px 24px;
  border-top: 5px solid var(--accent);
  background: linear-gradient(145deg, #f6f5ed, #fffdfa);
}
.obit-home {
  font-size: 11px;
  letter-spacing: 0.5px;
  color: var(--accent);
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 7px;
  margin-bottom: 35px;
}
.obit-home > span {
  font-family: Georgia, serif;
  font-size: 18px;
}
.obit-home img {
  height: 24px;
  max-width: 120px;
  object-fit: contain;
}
.obit-header .eyebrow {
  font-size: 9px;
  letter-spacing: 2.8px;
  color: #9d9d89;
}
.flower {
  font-size: 53px;
  color: #8e9e79;
  line-height: 1.4;
  font-family: serif;
}
.obit-header h1 {
  font-family: "Noto Serif KR", serif;
  font-size: 21px;
  font-weight: 500;
  margin: 8px 0 5px;
  letter-spacing: -1px;
}
.deceased {
  text-align: center;
  padding: 30px 32px;
}
.deceased > span {
  font-family: "Noto Serif KR", serif;
  color: #939b87;
  font-size: 17px;
}
.deceased h2 {
  font-family: "Noto Serif KR", serif;
  font-size: 31px;
  font-weight: 600;
  letter-spacing: 3px;
  margin: 12px 0;
}
.deceased h2 small {
  display: block;
  letter-spacing: 0;
  font-family: "Noto Sans KR", sans-serif;
  font-size: 12px;
  font-weight: 400;
  color: #8d9685;
  margin-top: 10px;
}
.deceased > p:not(.greeting) {
  font-size: 11px;
  color: #8e9784;
  line-height: 1.9;
}
.greeting {
  font-family: "Noto Serif KR", serif;
  font-size: 13px;
  line-height: 2.1;
  margin: 25px 0 2px;
  white-space: pre-line;
  color: #797f6c;
}
.portrait {
  width: 132px;
  height: 170px;
  object-fit: cover;
  display: block;
  margin: 0 auto 24px;
  border: 4px solid #eeeee4;
  box-shadow: 0 4px 12px #00000012;
}
.obit-section {
  margin: 0 32px;
  padding: 26px 0;
  border-top: 1px solid #e6e8dd;
}
.obit-section h3 {
  font-size: 13px;
  letter-spacing: 1px;
  color: var(--accent);
  font-weight: 600;
  margin: 0 0 21px;
}
.mourner-line {
  display: flex;
  align-items: center;
  gap: 18px;
  margin: 16px 0;
  line-height: 1.7;
  flex-wrap: wrap;
}
.mourner-line > span {
  font-size: 11px;
  color: #919982;
  min-width: 40px;
  max-width: 100px;
}
.mourner-line > strong {
  font-weight: 500;
  font-size: 14px;
  flex: 1;
  min-width: 70px;
}
.contact-pill {
  border: 1px solid #e0e6d8;
  border-radius: 20px;
  padding: 5px 10px;
  color: #829273;
  font-size: 10px;
}
.funeral-details {
  display: grid;
  grid-template-columns: 45px minmax(0, 1fr);
  gap: 18px 12px;
  font-size: 12px;
  line-height: 1.9;
  margin: 0;
}
.funeral-details dt {
  color: #98a088;
  font-size: 11px;
}
.funeral-details dd {
  margin: 0;
}
.funeral-details strong {
  font-size: 14px;
  font-weight: 500;
}
.accent {
  color: var(--accent);
  font-weight: 500;
}
.obit-section > strong {
  font-size: 13px;
  font-weight: 500;
}
.obit-section > .muted {
  font-size: 11px;
}
.map-card {
  border: 1px solid #e5e9df;
  background:
    radial-gradient(ellipse at 50% 40%, #edf1e5, transparent),
    repeating-linear-gradient(
      25deg,
      #f6f7f0,
      #f6f7f0 25px,
      #e9eddf 26px,
      #f6f7f0 27px
    );
  border-radius: 7px;
  padding: 22px;
  text-align: center;
  margin: 17px 0 13px;
}
.map-card > span {
  font-size: 27px;
  color: var(--accent);
}
.map-card p {
  font-size: 10px;
  color: #9ba88c;
}
.map-card .btn {
  font-size: 11px;
  padding: 9px 14px;
}
.account {
  border: 1px solid #e5e8df;
  border-radius: 6px;
  margin: 10px 0;
  padding: 14px;
  font-size: 12px;
}
.account summary {
  list-style: none;
  cursor: pointer;
  display: flex;
  gap: 12px;
  justify-content: space-between;
}
.account summary > span {
  font-size: 10px;
  color: #8d9d7e;
}
.account > p {
  letter-spacing: 0.3px;
  font-size: 12px;
}
.account > div {
  font-size: 11px;
  color: #8f9c83;
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
}
.obit-notice {
  font-size: 10px;
  color: #a0a58f;
  text-align: center;
  line-height: 1.9;
  padding: 20px 30px;
  margin: 0;
}
.obit-share {
  text-align: center;
  padding: 10px 0 30px;
}
.obit-footer {
  text-align: center;
  letter-spacing: 3px;
  font-family: Georgia, serif;
  font-size: 14px;
  color: #96a18b;
  background: #f2f3ea;
  padding: 27px;
}
.obit-footer small {
  display: block;
  font-size: 9px;
  font-family: "Noto Sans KR", sans-serif;
  letter-spacing: 1px;
  margin-top: 9px;
  color: #acb29d;
}
.memorial-portrait {
  width: 160px;
  margin: 23px auto 24px;
  position: relative;
}
.portrait-frame {
  padding: 7px;
  border: 1px solid #c7c5af;
  background: #fffdf8;
  box-shadow: 0 5px 20px #3c42220d;
  position: relative;
}
.portrait-frame:before,
.portrait-frame:after {
  content: "";
  position: absolute;
  width: 18px;
  height: 18px;
  border-color: #a8ad8d;
  pointer-events: none;
}
.portrait-frame:before {
  left: -4px;
  top: -4px;
  border-top: 2px solid;
  border-left: 2px solid;
}
.portrait-frame:after {
  bottom: -4px;
  right: -4px;
  border-right: 2px solid;
  border-bottom: 2px solid;
}
.portrait-image {
  display: block;
  width: 100%;
  aspect-ratio: 3/4;
  object-fit: cover;
  object-position: center 30%;
}
.memorial-art {
  aspect-ratio: 3/4;
  background: radial-gradient(ellipse at 50% 40%, #fffefb, #f0eee3);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  color: #8c9975;
  position: relative;
  overflow: hidden;
}
.memorial-art svg {
  width: 100%;
  height: 90%;
  position: absolute;
  inset: 0 0 auto;
}
.memorial-art > span {
  position: absolute;
  bottom: 13px;
  left: 0;
  right: 0;
  font-family: "Noto Serif KR", serif;
  font-size: 8px;
  letter-spacing: 1.3px;
  color: #9ba186;
}
.obit-header > .eyebrow {
  margin-top: 0;
}
.obit-header h1 {
  margin-top: 25px;
}

.deceased{padding-top:24px}
@media(max-width:600px){.public-page{padding:0;background:#fffdfa}.obituary{max-width:100%;box-shadow:none}.obit-header{padding-top:29px}.obit-header h1{font-size:20px}.obit-section{margin:0 26px}.deceased{padding:28px}}@media print{.obit-share,.products,.copy{display:none}.public-page{padding:0}.obituary{box-shadow:none;max-width:100%}.obit-section{break-inside:avoid}}


"""
