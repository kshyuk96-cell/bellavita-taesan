/* Batch 03: local admin assistance. No storage, retries or credential renewal. */
(() => {
  const forms=[...document.querySelectorAll('form[method=post]')];
  const originalButtons=new Map(),baselines=new Map();
  const snapshot=form=>JSON.stringify([...form.elements].filter(el=>el.name&&!el.disabled&&!el.readOnly&&!['hidden','submit','button'].includes(el.type)).map(el=>[el.name,el.type==='checkbox'||el.type==='radio'?el.checked:el.value]));
  let submitting=false,expired=false;
  for(const form of forms) {
    baselines.set(form,snapshot(form));
    for(const button of form.querySelectorAll('button:not([type]),button[type=submit],input[type=submit]'))originalButtons.set(button,button.disabled);
    const status=document.createElement('p');status.className='admin-form-status';status.setAttribute('role','status');status.hidden=true;form.append(status);
    const update=()=> {
      const dirty=snapshot(form)!==baselines.get(form);form.classList.toggle('admin-form-dirty',dirty);
      status.textContent=dirty?'변경 내용을 아직 저장하지 않았습니다.':'';status.hidden=!dirty;
    };
    form.addEventListener('input',update);form.addEventListener('change',update);
    form.addEventListener('submit',event=> {
      if(event.defaultPrevented)return;
      if(submitting||expired) { event.preventDefault();status.hidden=false;status.textContent=expired?'관리자 링크가 만료됐습니다. 새 링크를 요청하세요.':'저장 응답을 기다리고 있습니다. 반복 요청하지 마세요.';return; }
      submitting=true;
      if(event.submitter?.name) {
        const value=document.createElement('input');value.type='hidden';value.name=event.submitter.name;value.value=event.submitter.value;value.dataset.guardedSubmitter='1';form.append(value);
      }
      for(const button of originalButtons.keys())button.disabled=true;
      status.hidden=false;status.textContent='저장 응답을 기다리고 있습니다. 반복 요청하지 마세요.';
    });
  }
  window.addEventListener('beforeunload',event=> {
    if(!submitting&&forms.some(form=>snapshot(form)!==baselines.get(form))) {event.preventDefault();event.returnValue='';}
  });
  const banner=document.querySelector('[data-admin-expiry]');
  const started=performance.now();
  let phase='';
  function session() {
    if(!banner)return;
    const remaining=Math.max(0,Math.ceil(Number(banner.dataset.adminExpiry)-Number(banner.dataset.adminNow)-(performance.now()-started)/1000));
    expired=remaining===0;
    banner.querySelector('.admin-session-time').textContent=expired?'관리자 링크 만료':'관리자 링크 남은 시간 '+Math.floor(remaining/60)+'분 '+String(remaining%60).padStart(2,'0')+'초';
    const next=expired?'expired':remaining<=120?'soon':'normal';
    if(next!==phase) {
      phase=next;banner.classList.toggle('soon',phase==='soon');banner.classList.toggle('expired',expired);
      banner.querySelector('.admin-session-notice').textContent=expired?'입력 내용은 이 화면에서 확인·복사할 수 있습니다. 대표 카카오에서 쇼핑몰 관리를 다시 요청하세요. 이 화면은 자동 갱신하지 않습니다.':phase==='soon'?'만료가 가까워졌습니다. 새 관리자 링크를 준비하세요. 입력은 자동 저장되지 않습니다.':'새 관리자 링크는 대표 카카오에서 쇼핑몰 관리를 요청하세요. 단축키 Ctrl+K 또는 ⌘+K: 주문 검색';
    }
    if(expired)for(const button of originalButtons.keys())button.disabled=true;
  }
  session();if(banner)setInterval(session,1000);
  window.addEventListener('pageshow',()=> {
    submitting=false;
    for(const [button,disabled] of originalButtons)button.disabled=disabled;
    document.querySelectorAll('[data-guarded-submitter]').forEach(el=>el.remove());session();
    for(const form of forms)form.dispatchEvent(new Event('change'));
  });
  const search=document.querySelector('.admin-search');
  let searchDirty=false;
  const searchStatus=document.createElement('p');searchStatus.className='search-unapplied';searchStatus.setAttribute('role','status');
  if(search) {
    const initial=snapshot(search);search.after(searchStatus);
    const changed=()=> {searchDirty=snapshot(search)!==initial;searchStatus.textContent=searchDirty?'변경한 검색 조건이 아직 적용되지 않았습니다. 검색을 눌러 주세요.':'';};
    search.addEventListener('input',changed);search.addEventListener('change',changed);
  }
  document.addEventListener('keydown',event=> {
    if((event.ctrlKey||event.metaKey)&&!event.altKey&&event.key.toLowerCase()==='k'&&!document.querySelector('dialog[open]')) {
      const input=search?.querySelector('[name=q]');if(input) {event.preventDefault();input.scrollIntoView({block:'center'});input.focus();input.select();}
    }
  });
  for(const root of document.querySelectorAll('[data-shop-order] > details')) {
    const nav=document.createElement('nav');nav.className='order-inner-nav';nav.setAttribute('aria-label','주문 상세 빠른 이동');
    for(const [selector,label] of [['.order-focus','핵심 정보'],['.installation-copy','설치 정보'],['.order-reference','접수·결제 기록'],['.order-timeline','업무 이력'],['.order-tasks','담당 업무'],['.operator-handover','인수인계']]) {
      const target=root.querySelector(selector);if(!target)continue;
      const button=document.createElement('button');button.type='button';button.textContent=label;
      button.addEventListener('click',()=> {for(let parent=target;parent&&parent!==root;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;target.tabIndex=-1;target.scrollIntoView({block:'start'});target.focus({preventScroll:true});});nav.append(button);
    }
    root.querySelector('summary').after(nav);
  }
  const link=document.querySelector('.filtered-order-export');if(!link)return;
  const dialog=document.createElement('dialog');dialog.className='admin-export-dialog';dialog.setAttribute('aria-labelledby','admin-export-title');
  const make=(tag,text)=>{const el=document.createElement(tag);el.textContent=text;return el;};
  const title=make('h2','CSV 다운로드 전 확인');title.id='admin-export-title';dialog.append(title);
  const count=make('p','조회 시점 기준 '+link.dataset.exportCount+'건 · 최대 10,000건');dialog.append(count);
  dialog.append(make('p','새 주문이나 상태 변경으로 실제 다운로드 건수는 달라질 수 있습니다. 연락처는 일부 가립니다.'));
  const conditions=make('p','');dialog.append(conditions);
  const fieldset=document.createElement('fieldset');fieldset.append(make('legend','내보낼 항목'));
  for(const [key,text] of JSON.parse(link.dataset.exportFields)) {
    const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.value=key;input.checked=true;input.disabled=key==='order_id';label.append(input,document.createTextNode(' '+text+(key==='order_id'?' (필수)':'')));fieldset.append(label);
  }
  dialog.append(fieldset);
  const status=make('p','');status.setAttribute('role','status');dialog.append(status);
  const actions=make('div','');actions.className='export-actions';
  const cancel=make('button','취소');cancel.type='button';cancel.addEventListener('click',()=>dialog.close());
  const confirm=make('a','확인 후 CSV 받기');confirm.className='csv-confirm';actions.append(cancel,confirm);dialog.append(actions);document.body.append(dialog);
  function exportState() {
    const blocked=expired||searchDirty||Number(link.dataset.exportCount)>10000;
    const url=new URL(link.href,location.href);url.searchParams.set('columns',[...fieldset.querySelectorAll('input:checked')].map(el=>el.value).join(','));confirm.href=url.href;
    confirm.setAttribute('aria-disabled',String(blocked));
    status.textContent=expired?'관리자 링크가 만료됐습니다.':searchDirty?'검색 조건을 먼저 적용하세요.':Number(link.dataset.exportCount)>10000?'검색 조건을 좁혀 10,000건 이하로 조회하세요.':'고객정보가 포함된 파일입니다. 사용 후 안전하게 관리하세요.';
  }
  fieldset.addEventListener('change',exportState);
  link.addEventListener('click',event=> {
    event.preventDefault();session();
    if(searchDirty) {searchStatus.textContent='검색 조건을 먼저 적용한 뒤 CSV를 받으세요.';search.querySelector('button').focus();return;}
    conditions.textContent='적용 조건: '+([...document.querySelectorAll('.active-order-filters a')].map(el=>el.textContent.replace(/ ×$/,'')).join(' / ')||'전체 주문');
    exportState();dialog.showModal();
  });
  confirm.addEventListener('click',event=> {session();exportState();if(confirm.getAttribute('aria-disabled')==='true')event.preventDefault();});
})();
