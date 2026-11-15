/* Batch 04: local guidance only; server validation remains authoritative. */
window.BellavitaCheckoutGuidance = ({form,field,node,label,focusField,summary,mobile,refresh}) => {
  const selects=[...form.querySelectorAll('select[name^="choice"]')];
  const controls=[...form.elements].filter(el=>el.name&&['INPUT','TEXTAREA','SELECT'].includes(el.tagName)&&!['hidden','button','submit'].includes(el.type));
  const touched=new Set(),notes=new Map(),markers=new Map();
  const started=performance.now(),serverNow=Number(form.dataset.serverNow)*1000;
  const now=()=>serverNow+(performance.now()-started);
  const money=value=>value.toLocaleString('ko-KR')+'원';
  const text=(el,value)=>{if(el.textContent!==value)el.textContent=value;};
  const describe=(control,id)=>control.setAttribute('aria-describedby',[control.getAttribute('aria-describedby'),id].filter(Boolean).join(' '));
  const base=Number(form.dataset.basePrice);
  const total=()=>selects.reduce((sum,select)=>sum+Number(select.selectedOptions[0]?.dataset.amount||0),base);
  const price=node('div','',{class:'checkout-breakdown',id:'price-breakdown'});
  price.append(node('h3','금액 구성'));const amounts=node('ul','');price.append(amounts);
  const difference=node('p','',{id:'option-price-change',role:'status','aria-live':'polite'});price.append(difference);
  document.getElementById('amount').closest('section').append(price);
  let previousTotal=total();
  for(const select of selects)select.addEventListener('change',()=> {
    const current=total(),delta=current-previousTotal;
    text(difference,'이전 '+money(previousTotal)+' → 현재 '+money(current)+' · '+(delta>0?money(delta)+' 증가':delta<0?money(-delta)+' 감소':'금액 변동 없음'));
    previousTotal=current;
  });
  const eventInput=field('event_time');
  const eventNote=node('p','',{id:'event-countdown',class:'checkout-event-note',role:'status','aria-live':'polite'});
  if(eventInput)eventInput.closest('label').after(eventNote);
  else form.querySelector('.venue')?.after(eventNote);
  const address=field('address');
  if(address) {
    const counter=node('small','',{id:'address-count',class:'preflight-count'});address.after(counter);describe(address,counter.id);
  }
  for(const [index,control] of controls.entries()) {
    const parent=control.closest('label');if(!parent)continue;
    const marker=node('small','',{class:'field-requirement','aria-hidden':'true'});parent.append(marker);
    const note=node('small','',{class:'field-error',id:'field-error-'+index,hidden:''});control.after(note);describe(control,note.id);
    const fieldMarkers=[marker],fieldNotes=[note],step=parent.nextElementSibling;
    if(step?.classList.contains('mobile-choice')) {
      const mobileMarker=marker.cloneNode();step.querySelector('summary').append(mobileMarker);fieldMarkers.push(mobileMarker);
      const mobileNote=note.cloneNode();mobileNote.id+='-mobile';step.append(mobileNote);fieldNotes.push(mobileNote);describe(step.querySelector('summary'),mobileNote.id);
    }
    markers.set(control,fieldMarkers);notes.set(control,fieldNotes);
    control.addEventListener('blur',()=>{touched.add(control);refresh();});
  }
  for(const name of ['sender','buyer','venue','address']) {
    const control=field(name);if(!control)continue;
    control.addEventListener('blur',()=> {
      if(control.disabled||control.readOnly)return;
      const clean=control.value.trim();
      if(clean!==control.value){control.value=clean;control.dispatchEvent(new Event('input',{bubbles:true}));}
    });
  }
  const optionNotes=new Map();
  for(const select of selects) {
    if(![...select.options].some(option=>option.dataset.kg||/(?:^|\s)[234]호/.test(option.textContent)))continue;
    const parent=select.closest('label'),step=parent.nextElementSibling;
    const note=node('p','',{class:'option-explanation'});parent.append(note);const hints=[note];
    if(step?.classList.contains('mobile-choice')){const mobileNote=node('p','',{class:'option-explanation mobile-option-explanation'});step.after(mobileNote);hints.push(mobileNote);}
    optionNotes.set(select,hints);
  }
  const nextNote=node('p','',{class:'next-field-status',role:'status'});
  document.getElementById('order-progress').after(nextNote);
  const goNext=()=> {
    validate();const pending=[...form.elements].find(el=>el.willValidate&&!el.validity.valid);
    if(pending){touched.add(pending);render(false);text(nextNote,'다음 확인: '+label(pending));focusField(pending);}
    else {text(nextNote,'필수 입력을 확인했습니다. 주문 내용을 확인해 주세요.');summary.scrollIntoView({block:'start'});summary.focus({preventScroll:true});}
  };
  const next=node('button','다음 필수 입력',{type:'button',id:'next-required'});next.addEventListener('click',goNext);nextNote.after(next);
  const mobileNext=node('button','다음 필수 입력',{type:'button',id:'mobile-next-required'});mobileNext.addEventListener('click',goNext);mobile.append(mobileNext);
  const style=node('style',`
    .checkout-breakdown{margin-top:16px;padding:12px;background:#f3f6ee;border-radius:8px}
    .checkout-breakdown ul{padding-left:20px;overflow-wrap:anywhere}.checkout-breakdown h3{font-size:1rem}
    .field-requirement{display:block;font-size:.8rem;font-weight:400;color:#53644e;margin:3px 0}
    .field-error{display:block;color:#96382c;font-size:.88rem;font-weight:400;overflow-wrap:anywhere}
    .field-error[hidden],.field-requirement[hidden]{display:none}
    #order [aria-invalid=true]{border-color:#a93828;outline:1px solid #a93828}
    .option-explanation,.checkout-event-note{font-size:.9rem;color:#425e3c;overflow-wrap:anywhere}.mobile-option-explanation{display:none}
    #next-required{width:auto;min-height:44px;margin-bottom:12px}
    @media(max-width:670px){body{padding-bottom:calc(142px + env(safe-area-inset-bottom))}.preflight-mobile{flex-wrap:wrap}.preflight-mobile strong{flex:1 1 100%}.preflight-mobile button{flex:1;max-width:none}#next-required{display:none}.mobile-option-explanation{display:block}}
  `);document.head.append(style);
  function validate() {
    for(const name of ['sender','buyer','venue','address']) {
      const control=field(name);if(control&&!control.disabled)control.setCustomValidity(control.value&&!control.value.trim()?'공백만 입력할 수 없습니다.':'');
    }
    if(eventInput) {
      eventInput.setCustomValidity('');
      if(eventInput.value) {
        const when=Date.parse(eventInput.value+'+09:00');
        if(!Number.isFinite(when)||when<=now())eventInput.setCustomValidity('행사 일시는 한국 시간 기준 현재 이후로 입력해 주세요.');
      }
    }
  }
  function render(showAll) {
    amounts.replaceChildren(node('li','기본 판매가 '+money(base)));
    for(const select of selects)if(select.value)amounts.append(node('li',label(select)+' · 추가금 '+money(Number(select.selectedOptions[0].dataset.amount||0))));
    amounts.append(node('li','합계 '+money(total())));
    if(address)text(document.getElementById('address-count'),[...address.value].length+' / '+address.maxLength+'자');
    for(const [control,fieldMarkers] of markers) {
      for(const marker of fieldMarkers){marker.hidden=control.disabled||Boolean(control.closest('[hidden]'));text(marker,control.readOnly?'자동 입력':control.required?'필수':'선택');}
      const invalid=control.willValidate&&!control.validity.valid&&(showAll||touched.has(control));
      for(const note of notes.get(control)){note.hidden=!invalid;if(invalid)text(note,control.validity.valueMissing?(control.type==='checkbox'?'필수 동의 항목을 확인해 주세요.':label(control)+(control.tagName==='SELECT'?': 필수 항목입니다. 선택해 주세요.':': 필수 항목입니다. 입력해 주세요.')):control.validationMessage);}
      if(invalid){control.setAttribute('aria-invalid','true');}
      else if(control.validity.valid||!control.willValidate)control.removeAttribute('aria-invalid');
    }
    for(const [select,hints] of optionNotes) {
      const option=select.selectedOptions[0],kg=option?.dataset.kg,frame=option?.textContent.match(/(?:^|\s)([234])호/);
      for(const note of hints)text(note,!select.value?'쌀 중량과 액자 호수는 상품 선택 항목과 상세 안내를 확인해 주세요.':kg?'선택한 쌀 중량: '+kg+'kg · 설치·교환 조건은 상품 상세 안내를 확인해 주세요.':frame?'선택한 액자 호수: '+frame[1]+'호 · 크기·구성은 선택 항목과 상세 안내를 확인해 주세요.':'선택한 구성은 상품 상세 안내와 함께 확인해 주세요.');
    }
    const value=eventInput?.value||form.dataset.eventTime;
    const when=eventInput?Date.parse(value+'+09:00'):Date.parse(value);
    if(!value||!Number.isFinite(when)){text(eventNote,'행사 일시는 한국 시간 기준으로 입력해 주세요.');return;}
    const day=value=>new Date(value+9*3600000).toISOString().slice(0,10);
    const days=Math.round((Date.parse(day(when)+'T00:00:00Z')-Date.parse(day(now())+'T00:00:00Z'))/86400000);
    text(eventNote,when<=now()?'행사 시각이 지났습니다. 일시와 주문 가능 여부를 확인해 주세요.':days===0?'오늘 행사 · 한국 시간 기준':days===1?'내일 행사 · 한국 시간 기준':'행사까지 '+days+'일 · 한국 시간 기준');
  }
  function reset(){touched.clear();previousTotal=total();text(difference,'');text(nextNote,'');}
  setInterval(refresh,30000);
  return {validate,render,reset};
};
