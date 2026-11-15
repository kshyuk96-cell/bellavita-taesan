/* Payment-independent checkout assistance; no storage or provider requests. */
(() => {
  const form = document.getElementById('order');
  if (!form) return;
  const field = name => form.elements.namedItem(name);
  const selects = [...form.querySelectorAll('select[name^="choice"]')];
  const submit = form.querySelector('button[type="submit"]');
  const phone = field('buyer_phone');
  const receiptType = field('cash_receipt_type');
  const receiptId = field('cash_receipt_identifier');
  const cleanNumber = value => value.replace(/[\s-]/g, '');
  const node = (tag, text, attrs = {}) => {
    const el = document.createElement(tag); el.textContent = text;
    for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
    return el;
  };
  const style = node('style', `
    .preflight-review dl { margin: 12px 0; }
    .preflight-review dt { font-weight: 700; margin-top: 12px; }
    .preflight-review dd { margin: 3px 0; white-space: pre-wrap; overflow-wrap: anywhere; }
    .preflight-review button,.preflight-errors button { width: auto; min-height: 44px; padding: 6px 12px; margin: 4px 0; }
    .preflight-count { display: block; font-size: .85rem; font-weight: 400; color: #526052; }
    .preflight-errors { border: 2px solid #9b3427; padding: 12px; border-radius: 8px; }
    .preflight-errors h2 { color: #842b20; }
    .preflight-progress { color: #354d3a; }
    .preflight-mobile { display: none; }
    .preflight-reset { background: white; color: #354d3a; border: 1px solid #a0aaa0; }
    .preflight-reset-dialog { max-width: min(90vw, 420px); border: 1px solid #a0aaa0; border-radius: 12px; }
    .preflight-reset-dialog::backdrop { background: #0007; }
    @media (max-width: 670px) {
      body { padding-bottom: calc(92px + env(safe-area-inset-bottom)); }
      .preflight-mobile { position: fixed; bottom: 0; left: 0; right: 0; z-index: 20;
        display: flex; gap: 12px; align-items: center; justify-content: space-between;
        padding: 10px 16px calc(10px + env(safe-area-inset-bottom)); background: #fff; border-top: 1px solid #bac4b9; }
      .preflight-mobile button { width: auto; margin: 0; }
      .preflight-mobile strong { overflow-wrap: anywhere; }
      body:has(dialog[open]) .preflight-mobile { visibility: hidden; }
    }
  `);
  document.head.append(style);
  const errors = node('section', '', {class:'preflight-errors', id:'order-errors', hidden:'', tabindex:'-1', 'aria-labelledby':'order-errors-title'});
  errors.append(node('h2', '입력 내용을 확인해 주세요', {id:'order-errors-title'}));
  const errorList = node('ul', ''); errors.append(errorList); form.prepend(errors);
  const errorRows = new Map();
  const progress = node('p', '', {class:'preflight-progress', id:'order-progress', role:'status', 'aria-live':'polite'});
  form.querySelector('.product-options').prepend(progress);
  const summary = node('section', '', {class:'preflight-review', id:'order-review', tabindex:'-1', 'aria-labelledby':'order-review-title'});
  summary.append(node('h2', '주문 전 확인', {id:'order-review-title'}));
  summary.append(node('p', '입력한 상품·문구·설치 정보를 확인해 주세요.'));
  const list = node('dl', ''); summary.append(list);
  const summaryRows = [];
  const reset = node('button', '입력 내용 지우기', {type:'button', class:'preflight-reset'}); summary.append(reset);
  const paymentSection = document.getElementById('amount').closest('section');
  paymentSection.before(summary);
  const mobile = node('aside', '', {class:'preflight-mobile', 'aria-label':'주문 금액과 확인'});
  const mobileAmount = node('strong', '');
  const reviewButton = node('button', '주문 내용 확인', {type:'button'});
  mobile.append(mobileAmount, reviewButton); document.body.append(mobile);
  reviewButton.addEventListener('click', () => { summary.scrollIntoView({block:'start'}); summary.focus({preventScroll:true}); });

  function label(control) {
    if (control.name === 'consent') return '주문 정보 수집 동의';
    const parent = control.closest('label');
    return parent ? parent.childNodes[0].textContent.trim() : control.name;
  }
  function focusField(control) {
    if (!control) return;
    if (control === field('buyer') && control.readOnly) control = field('same_buyer');
    const step = control.closest('label')?.nextElementSibling;
    if (step?.classList.contains('mobile-choice') && matchMedia('(max-width:670px)').matches) {
      step.open = true; step.scrollIntoView({block:'center'}); step.querySelector('summary').focus(); return;
    }
    control.scrollIntoView({block:'center'}); control.focus({preventScroll:true});
  }
  for (const name of ['sender', 'message']) {
    const control = field(name);
    const counter = node('small', '', {class:'preflight-count', id:name+'-count'});
    control.after(counter);
    control.setAttribute('aria-describedby', [control.getAttribute('aria-describedby'), counter.id].filter(Boolean).join(' '));
  }
  phone.setAttribute('inputmode', 'tel');
  phone.placeholder = '010-1234-5678';
  const guidance=window.BellavitaCheckoutGuidance({form,field,node,label,focusField,summary,mobile,refresh:()=>render()});
  function validate() {
    const number = cleanNumber(phone.value.trim());
    phone.setCustomValidity(number && !/^01[016789]\d{7,8}$/.test(number) ? '휴대폰 번호를 확인해 주세요. 예: 010-1234-5678' : '');
    receiptId.setCustomValidity('');
    if (!receiptType.disabled) {
      receiptId.disabled = receiptType.value === 'self';
      receiptId.required = receiptType.value === 'business';
      if (!receiptId.disabled) {
        const id = cleanNumber(receiptId.value.trim());
        const effective = id || (receiptType.value === 'personal' ? number : '');
        if (effective && !(receiptType.value === 'business' ? /^\d{10}$/ : /^01[016789]\d{8}$/).test(effective))
          receiptId.setCustomValidity(receiptType.value === 'business' ? '사업자번호 10자리를 입력해 주세요.' : '현금영수증용 휴대폰 번호 11자리를 확인해 주세요.');
      }
    }
    guidance.validate();
  }
  function invalidFields() { return [...form.elements].filter(el => el.willValidate && !el.validity.valid); }
  function showErrors() {
    validate();
    const invalid = invalidFields();
    for (const [control,item] of errorRows) if (!invalid.includes(control)) { item.remove(); errorRows.delete(control); }
    for (const control of invalid) {
      control.setAttribute('aria-invalid','true');
      let item=errorRows.get(control);
      if (!item) {
        item=node('li',''); const link=node('button','',{type:'button'});
        link.addEventListener('click',()=>focusField(control)); item.append(link);errorRows.set(control,item);errorList.append(item);
      }
      item.firstChild.textContent=label(control)+': '+control.validationMessage;
    }
    errors.hidden = invalid.length === 0;
    return invalid;
  }
  let errorsShown = false;
  let invalidTimer;
  form.addEventListener('invalid', () => {
    errorsShown = true; clearTimeout(invalidTimer);
    invalidTimer = setTimeout(() => { showErrors(); }, 0);
  }, true);
  function render() {
    validate(); let rowIndex=0;
    const row = (title, text, control) => {
      let entry=summaryRows[rowIndex++];
      if (!entry) {
        const dt=node('dt',''),dd=node('dd',''),value=node('span',''),edit=node('button','',{type:'button'});
        dd.append(value,document.createTextNode(' '),edit);list.append(dt,dd);
        entry={dt,dd,value,edit};summaryRows.push(entry);
      }
      entry.dt.textContent=title;entry.value.textContent=text || '아직 입력하지 않았습니다';
      entry.edit.hidden=!control || control.disabled;
      entry.edit.textContent=title+' 수정';entry.edit.onclick=()=>focusField(control);
    };
    row('상품',form.querySelector('.product-options h2').textContent);
    for (const select of selects) if (select.id !== 'greeting-choice')
      row(label(select),select.value ? select.selectedOptions[0].textContent : '', select);
    const greeting = selects.find(select => select.id === 'greeting-choice');
    const direct = !field('message').disabled;
    row('축하 문구',direct ? field('message').value.trim() : greeting?.selectedOptions[0]?.dataset.greeting || '', direct ? field('message') : greeting);
    for (const name of ['sender','buyer','buyer_phone']) {
      const text = field(name).value.trim() + (name === 'buyer' && field(name).readOnly ? ' (보내시는 분과 같음)' : '');
      row(name === 'buyer' ? '주문자 이름' : label(field(name)),text,field(name));
    }
    for (const control of form.querySelectorAll('input[name^="extra"]')) row(label(control),control.value,control);
    if (field('venue')) {
      for (const name of ['venue','address','event_time']) row(label(field(name)),field(name).value.replace('T',' '),field(name));
    } else row('설치 장소·일시',form.querySelector('.venue')?.innerText.trim());
    if (!receiptType.disabled) row('현금영수증',receiptType.selectedOptions[0].textContent,receiptType);
    row('총 결제금액',document.getElementById('amount').textContent);
    while (summaryRows.length>rowIndex) { const entry=summaryRows.pop();entry.dt.remove();entry.dd.remove(); }
    mobileAmount.textContent = document.getElementById('amount').textContent;
    for (const name of ['sender','message']) {
      const control = field(name); document.getElementById(name+'-count').textContent = [...control.value].length+' / '+control.maxLength+'자';
      control.closest('label').querySelector('.preflight-count').hidden = control.disabled;
    }
    const required = [...form.elements].filter(el => el.required && !el.disabled);
    const completed = required.filter(el => el.validity.valid).length;
    progress.textContent = '필수 입력 '+completed+' / '+required.length+' 완료';
    for (const control of form.elements) if (control.getAttribute('aria-invalid') === 'true' && control.validity.valid) control.removeAttribute('aria-invalid');
    if (errorsShown) showErrors();
    guidance.render(errorsShown);
  }
  function snapshot() {
    return JSON.stringify([...form.elements].filter(el => el.name && !['hidden','submit','button'].includes(el.type)).map(el => [el.name,el.type === 'checkbox' ? el.checked : el.value]));
  }
  let initial;
  let submitted = false;
  form.addEventListener('input',render);
  form.addEventListener('change',render);
  phone.addEventListener('blur', () => { phone.value = cleanNumber(phone.value.trim()); render(); });
  receiptId.addEventListener('blur', () => { receiptId.value = cleanNumber(receiptId.value.trim()); render(); });
  form.addEventListener('submit',event => {
    validate();
    if (invalidFields().length) {
      event.preventDefault(); submit.disabled = form.dataset.canSubmit !== 'true'; errorsShown=true;
      const invalid=showErrors(); focusField(invalid[0]); return;
    }
    if (submitted) { event.preventDefault(); return; }
    submitted = true;
  });
  window.addEventListener('beforeunload',event => {
    if (!submitted && snapshot() !== initial) { event.preventDefault(); event.returnValue=''; }
  });
  window.addEventListener('pageshow',() => { submitted=false; render(); });
  const dialog = node('dialog','',{class:'preflight-reset-dialog','aria-labelledby':'reset-order-title'});
  dialog.append(node('h2','입력 내용을 지울까요?',{id:'reset-order-title'}),node('p','선택한 옵션과 입력한 문구·연락처를 모두 지웁니다.'));
  const cancel=node('button','계속 입력하기',{type:'button',autofocus:''});
  const confirm=node('button','지우기',{type:'button'}); dialog.append(cancel,confirm); document.body.append(dialog);
  reset.addEventListener('click',()=> { if (!submitted) dialog.showModal(); });
  cancel.addEventListener('click',()=>dialog.close());
  confirm.addEventListener('click',()=> {
    form.reset();
    // Let the host checkout synchronize its own preview and buyer cache.
    form.dispatchEvent(new CustomEvent('bellavita:checkout-reset'));
    receiptType.dispatchEvent(new Event('change',{bubbles:true}));
    errorsShown=false; errors.hidden=true; submitted=false;
    for (const control of form.elements) control.removeAttribute('aria-invalid');
    guidance.reset();render(); initial=snapshot(); dialog.close(); focusField(selects[0] || field('sender'));
  });
  render(); initial=snapshot();
})();
