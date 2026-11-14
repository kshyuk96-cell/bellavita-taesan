"""Inline input guidance backed by authoritative server validation."""
SCRIPT=r'''
for(const action of document.querySelectorAll('input[name=action][value=save_product]')){
 const form=action.form;const guide=document.createElement('p');guide.className='product-guidance';guide.setAttribute('role','status');form.prepend(guide);
 const price=form.elements.namedItem('price');price.min='1';price.max='100000000';price.step='1';
 function check(){
  for(const element of form.elements)if(element.setCustomValidity)element.setCustomValidity('');
  for(const checkbox of form.querySelectorAll('input[name^=remove_group]')){const suffix=checkbox.name.replace('remove_group','');for(const prefix of ['group_label','group_choices','group_custom']){const control=form.elements.namedItem(prefix+suffix);if(control)control.disabled=checkbox.checked;}}
  const choices=Array.from(form.querySelectorAll('textarea[name=options],textarea[name^=group_choices],textarea[name=new_group_choices]'));
  const maxima=[];
  for(const input of choices){if(input.disabled)continue;const seen=new Set();let maximum=0;const lines=input.value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);let error='';
   for(const [index,line] of lines.entries()){const split=line.lastIndexOf('|');const label=line.slice(0,split).trim();const amount=line.slice(split+1).trim();if(split<1||!label||!/^\d{1,9}$/.test(amount)||Number(amount)>100000000){error=(index+1)+'번째 줄은 옵션명 | 추가금(0 이상의 원 단위)으로 입력하세요.';break;}const key=label.toLowerCase().replace(/\s+/g,' ');if(seen.has(key)){error=(index+1)+'번째 줄의 선택지 이름이 중복됩니다.';break;}seen.add(key);maximum=Math.max(maximum,Number(amount));}
   if(lines.length>60)error='선택지는 최대 60개입니다.';
   input.setCustomValidity(error);
   const suffix=input.name.replace('group_choices','');const removed=form.elements.namedItem('remove_group'+suffix)?.checked;
   if(!removed&&(input.name!=='new_group_choices'||form.elements.namedItem('new_group_label')?.value.trim()))maxima.push(maximum);
  }
  const added=form.elements.namedItem('new_group_label');const addedChoices=form.elements.namedItem('new_group_choices');
  if(added&&addedChoices){if(added.value.trim()&&!addedChoices.value.trim())addedChoices.setCustomValidity('추가할 묶음의 선택지도 입력하세요.');if(!added.value.trim()&&addedChoices.value.trim())added.setCustomValidity('추가할 묶음 이름도 입력하세요.');}
  const names=Array.from(form.querySelectorAll('input[name^=group_label],input[name=new_group_label]'));const seen=new Set();let remaining=0;
  for(const input of names){const suffix=input.name.replace('group_label','');if(form.elements.namedItem('remove_group'+suffix)?.checked)continue;const name=input.value.trim().replace(/\s+/g,' ').toLowerCase();if(!name)continue;remaining++;if(seen.has(name))input.setCustomValidity('옵션 묶음 이름이 중복됩니다.');seen.add(name);}
  const count=form.elements.namedItem('group_count');if(count&&remaining===0)price.setCustomValidity('옵션 묶음은 한 개 이상 남겨 주세요.');if(remaining>8)price.setCustomValidity('옵션 묶음은 최대 8개입니다.');
  if(Number(price.value)+maxima.reduce((a,b)=>a+b,0)>100000000)price.setCustomValidity('기본가와 옵션 추가금의 합계는 1억원까지입니다.');
  const errors=Array.from(form.elements).filter(el=>el.validity?.customError).map(el=>el.validationMessage);
  guide.textContent=errors.length?'입력 확인: '+Array.from(new Set(errors)).join(' / '):'기본 판매가에 선택한 옵션 추가금이 더해집니다. 선택지 이름은 서로 구분하고, 저장 전 변경 전후 비교를 확인하세요.';
 }
 form.addEventListener('input',check);form.addEventListener('change',check);check();
}
'''
