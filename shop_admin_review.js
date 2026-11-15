/* Batch 02: no browser storage, no provider calls and no order-state writes. */
(() => {
  const phoneTimers=new WeakMap();
  const mask = value => {
    const digits=value.replace(/[\s-]/g,'');
    return digits.length>=10 ? digits.slice(0,3)+'-****-'+digits.slice(-4) : '연락처 확인';
  };
  function hidePhone(box) {
    clearTimeout(phoneTimers.get(box));
    box.querySelector('.phone-value').textContent=mask(box.dataset.phone);
    const button=box.querySelector('.phone-toggle');button.textContent='전화번호 확인';button.setAttribute('aria-expanded','false');
  }
  for (const box of document.querySelectorAll('.admin-phone')) {
    box.querySelector('.phone-toggle').addEventListener('click',()=> {
      const button=box.querySelector('.phone-toggle');
      if (button.getAttribute('aria-expanded')==='true') return hidePhone(box);
      box.querySelector('.phone-value').textContent=box.dataset.phone;
      button.textContent='전화번호 가리기';button.setAttribute('aria-expanded','true');
      phoneTimers.set(box,setTimeout(()=>hidePhone(box),60000));
    });
    for (const detail of box.closest('[data-shop-order]').querySelectorAll('details'))
      detail.addEventListener('toggle',()=> { if (!detail.open) hidePhone(box); });
  }
  window.addEventListener('pagehide',()=>document.querySelectorAll('.admin-phone').forEach(hidePhone));
  document.addEventListener('visibilitychange',()=> { if(document.hidden)document.querySelectorAll('.admin-phone').forEach(hidePhone); });
  for (const button of document.querySelectorAll('[data-install-copy]')) button.addEventListener('click',async()=> {
    const area=button.closest('.installation-copy'),status=area.querySelector('[role=status]');
    try { await navigator.clipboard.writeText(button.dataset.installCopy);status.textContent='설치 정보를 복사했습니다.'; }
    catch(error) { const source=area.querySelector('textarea');source.focus();source.select();status.textContent='설치 정보를 선택했습니다. 직접 복사해 주세요.'; }
  });
  for (const form of document.querySelectorAll('form[method=post]')) form.addEventListener('submit',()=> {
    const order=form.closest('[data-shop-order]');if (!order)return;
    const opened=[...order.querySelectorAll('details')].map((detail,index)=>detail.open?index:null).filter(value=>value!==null).slice(0,12);
    let input=form.querySelector('[name=_return_details]');
    if (!input) { input=document.createElement('input');input.type='hidden';input.name='_return_details';form.append(input); }
    input.value=opened.join('.');
  });
  if (/^#order-[a-f0-9]{32}$/.test(location.hash)) {
    const expanded=new URLSearchParams(location.search).get('expanded')||'';
    if (/^\d{1,2}(?:\.\d{1,2}){0,11}$/.test(expanded)) {
      const details=document.getElementById(location.hash.slice(1))?.querySelectorAll('details');
      for (const index of expanded.split('.').map(Number)) if (details?.[index]) details[index].open=true;
    }
  }
})();
