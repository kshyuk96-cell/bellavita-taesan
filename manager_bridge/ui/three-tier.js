'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const endpoint = '/v1/representative/runtime/three-tier/overview';
  const stateLabels = {
    development_queued:'개발 대기', developing:'개발 중', candidate_ready:'검토 대기', reviewing:'검토 중',
    review_passed:'독립 검토 통과', review_failed:'검토 실패', rework_queued:'보완 대기',
    rework_developing:'보완 중', needs_verification:'실행 대조 필요', accepted:'기술 인수 완료', unknown:'확인 필요'
  };
  let bearer = '', cookieSession = false, generation = 0, viewRequest = 0, authQueue = Promise.resolve();
  let nextCursor=null, runtimeRequest=0;
  const pending = new Set();
  function el(tag,text,className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }
  function notice(text,bad=false) { $('tier-message').textContent=text; $('tier-message').className=bad?'error':''; }
  function wipe() {
    for (const id of ['tier-owner','tier-counts','tier-attention','tier-boards']) $(id).replaceChildren();
    nextCursor=null; $('tier-next').disabled=true;
    $('tier-refresh').disabled=true;
    $('tier-scope').textContent='인증한 프로젝트의 등록 기록만 표시합니다.';
  }
  function abortReads() { for (const controller of pending) controller.abort(); pending.clear(); }
  function reset() {
    generation++; viewRequest++; runtimeRequest++; $('tier-runtime').replaceChildren(); $('tier-runtime-read').disabled=true; bearer=''; cookieSession=false; abortReads(); wipe();
    $('tier-token').value=''; $('tier-pairing').value='';
  }
  function projects() {
    const values=$('tier-projects').value.split(/[,\n\r]+/).map(value=>value.trim()).filter(Boolean);
    if (!values.length || values.length>100 || new Set(values).size!==values.length || values.some(value=>! /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(value))) {
      throw Error('프로젝트 이름을 쉼표 또는 줄바꿈으로 구분해 중복 없이 입력해 주십시오.');
    }
    return values;
  }
  function queuedAuth(action) {
    const result=authQueue.catch(()=>{}).then(action); authQueue=result.catch(()=>{}); return result;
  }
  function authFetch(path,body) {
    return fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Taesan-Client':'dashboard'},
      body:JSON.stringify(body),credentials:'same-origin',cache:'no-store',redirect:'error',signal:AbortSignal.timeout(20000)});
  }
  async function overview(requested,cursor=null) {
    const epoch=generation, credential=bearer, cookie=cookieSession, serial=++viewRequest;
    const controller=new AbortController(); pending.add(controller);
    const timeout=setTimeout(()=>controller.abort(),20000);
    try {
      const headers={'Content-Type':'application/json','X-Taesan-Client':'dashboard'};
      if (credential) headers.Authorization='Bearer '+credential;
      const response=await fetch(endpoint,{method:'POST',headers,body:JSON.stringify(cursor?{projects:requested,cursor}:{projects:requested}),
        credentials:cookie?'same-origin':'omit',cache:'no-store',redirect:'error',signal:controller.signal});
      if (epoch!==generation || serial!==viewRequest || credential!==bearer || cookie!==cookieSession) return;
      if (!response.ok) {
        wipe();
        if ([401,403].includes(response.status)) {
          reset(); notice('관리 연결이 만료·해제됐거나 권한이 없어 화면을 비웠습니다.',true); return;
        }
        throw Error('프로젝트 권한·현재 후보·관리 연결을 확인하지 못했습니다. 자동 재요청하지 않습니다.');
      }
      const value=await response.json();
      if (epoch!==generation || serial!==viewRequest || credential!==bearer || cookie!==cookieSession) return;
      if (!value.ok || !value.result) throw Error('관리 현황을 확인하지 못했습니다.');
      if (value.result.state==='not_configured') {
        wipe(); notice('3단계 관리 서버가 아직 연결되지 않았습니다. 등록과 실제 연결은 다릅니다.',true); return;
      }
      render(value.result); $('tier-refresh').disabled=false; $('tier-runtime-read').disabled=false;
      nextCursor=typeof value.result.next_cursor==='string'&&/^[a-f0-9]{32}$/.test(value.result.next_cursor)?value.result.next_cursor:null; $('tier-next').disabled=!nextCursor;
      notice('현재 등록된 개발·검토 현황을 확인했습니다. 운영 반영은 실행하지 않았습니다.');
    } catch (error) {
      if (epoch!==generation || serial!==viewRequest) return;
      wipe(); notice(error.name==='AbortError'?'현황 조회가 지연돼 중단했습니다. 실행 상태를 임의로 판단하지 않습니다.':'관리 현황을 불러오지 못했습니다. 권한과 연결 상태를 확인해 주십시오.',true);
    } finally { clearTimeout(timeout); pending.delete(controller); }
  }
  function count(value) { return Number.isInteger(value)&&value>=0?value:'미측정'; }
  function safeText(value,fallback='미확인') { return typeof value==='string'&&value.length<=2000?value:fallback; }
  function itemList(container,items,empty,owner=false) {
    container.replaceChildren();
    if (!items.length) { container.append(el('p',empty)); return; }
    const list=el('ul');
    for (const item of items) {
      const line=safeText(item.project)+' / '+safeText(item.task_id)+': '+safeText(item.message)+(owner?' · 아직 승인되지 않았습니다.':'');
      list.append(el('li',line));
    }
    container.append(list);
  }
  function render(summary) {
    if (!summary || summary.operational_change!==false || summary.snapshot_scope!=='authorized_registered_hierarchy' || !Array.isArray(summary.projects) || summary.projects.length>100 || !Array.isArray(summary.owner_decisions) || !Array.isArray(summary.manager_attention)) throw Error('검증된 현황이 아닙니다.');
    if (summary.projects.some(project=>!project || !Array.isArray(project.tasks) || project.tasks.length>200)) throw Error('현황 범위를 확인하지 못했습니다.');
    if (summary.owner_decisions.length>20000 || summary.manager_attention.length>20000) throw Error('현황 범위를 확인하지 못했습니다.');
    wipe();
    itemList($('tier-owner'),summary.owner_decisions,'현재 전달된 기록에 형님 결정 요청이 없습니다.',true);
    itemList($('tier-attention'),summary.manager_attention,'현재 전달된 기록에 태산이의 추가 확인 항목이 없습니다.');
    const counters=summary.counts_displayed||{};
    for (const [label,value] of [
      ['개발 중',(Number.isInteger(counters.developing)&&Number.isInteger(counters.rework_developing))?counters.developing+counters.rework_developing:null],
      ['검토 대기',counters.candidate_ready],['검토 중',counters.reviewing],['기술 인수',summary.projects.reduce((total,project)=>total+project.tasks.filter(task=>task.state==='accepted'&&task.technical_accepted===true).length,0)],
      ['대표 결정',summary.owner_decision_count],['태산이 확인',summary.manager_attention_count]
    ]) {
      const card=el('div',undefined,'count'); card.append(el('span',label),el('strong',count(value))); $('tier-counts').append(card);
    }
    let truncated=false;
    for (const project of summary.projects) {
      const section=el('section'), title=safeText(project.name,safeText(project.project));
      section.append(el('h2',title));
      section.append(el('p','실제 연결 확인 '+count(project.connection_verified_members)+' / 등록 '+count(project.registered_members)+'명.'));
      const capacity=project.capacity||{};
      section.append(el('p',capacity.configured!==true?'작업 실행·검토 상한은 확인되지 않았습니다.':capacity.review_backlog_observed===true?'표시된 검토 대기가 상한에 도달했습니다.':'작업 실행·검토 상한이 설정돼 있습니다.','muted'));
      const scroll=el('div',undefined,'table-scroll'), table=el('table');
      table.append(el('caption',title+' · 기록된 전체 '+count(project.total_tasks)+'건 중 현재 '+project.tasks.length+'건 표시'));
      const thead=el('thead'), headings=el('tr');
      for (const label of ['작업','진행 상태','검증 후보','실제 검사 결과']) {const th=el('th',label);th.setAttribute('scope','col');headings.append(th);}
      thead.append(headings); table.append(thead); const tbody=el('tbody');
      for (const task of project.tasks) {
        const row=el('tr'), id=el('td',safeText(task.task_id)), status=el('td');
        id.append(el('br'),el('small','보완 '+count(task.rework_count)+'회'));
        let state=stateLabels[task.state]||'확인 필요';
        if (task.state==='accepted' && task.technical_accepted!==true) state=task.source_freshness==='not_checked'?'이전 인수 기록 · 현재 소스 최신성 미점검':'기술 인수 근거 재확인 필요';
        status.append(el('span',state),el('br'),el('small',safeText(task.blocked_label,'')));
        const candidate=el('td');candidate.append(el('code',safeText(task.head_sha,'후보 미확인')));
        const checks=task.checks||{}, evidence=el('td','통과 '+count(checks.passed)+' / 실패 '+count(checks.failed)+' / 미측정 '+count(checks.unmeasured));
        evidence.append(el('br'),el('small','운영 배포 미확인'));
        row.append(id,status,candidate,evidence);tbody.append(row);
      }
      if (!project.tasks.length) {const row=el('tr'), cell=el('td','현재 표시할 작업이 없습니다.');cell.setAttribute('colspan','4');row.append(cell);tbody.append(row);}
      table.append(tbody);scroll.append(table);section.append(scroll);
      if (project.tasks_truncated===true) {section.append(el('p','일부 작업만 표시했습니다. 전체 작업 건수로 해석하지 마십시오.','muted'));truncated=true;}
      $('tier-boards').append(section);
    }
    if (Number.isInteger(summary.total_tasks)) { $('tier-scope').textContent='기록된 전체 '+count(summary.total_tasks)+'건 중 현재 '+count(summary.displayed_tasks)+'건 표시. 결정·확인 건수는 현재 페이지 기준입니다. 소스 최신성 점검 '+count(summary.freshness_checks)+'건. 전체 Codex 채팅 목록이 아닙니다.'; return; }
    $('tier-scope').textContent=truncated?'일부 작업의 표시 건수입니다. 전체 작업 수가 아닙니다.':'인증한 프로젝트에서 현재 전달된 작업 기록입니다. 전체 Codex 채팅 목록이 아닙니다.';
  }
  $('tier-connect').addEventListener('submit',async event=>{
    event.preventDefault();
    const code=$('tier-pairing').value.trim(), legacy=$('tier-token').value.trim();
    reset();const epoch=generation;
    try {
      const requested=projects();
      if (code&&legacy) throw Error('연결 코드와 관리 토큰 중 하나만 입력해 주십시오.');
      if (code&&! /^[A-Za-z0-9]{12}$/.test(code)) throw Error('12자리 관리 연결 코드를 확인해 주십시오.');
      if (legacy && (legacy.length<32 || legacy.length>1024 || /\s/.test(legacy) || /^sk-/.test(legacy))) throw Error('이곳에는 전용 관리 토큰만 사용할 수 있습니다.');
      await queuedAuth(async()=>{
        if (code||legacy) {
          const loggedOut=await authFetch('/v1/auth/logout',{});
          if (!loggedOut.ok) throw Error('이전 관리 세션 종료를 확인하지 못했습니다.');
        }
        if (epoch!==generation) return;
        if (code) {
          const response=await authFetch('/v1/auth/redeem',{code});
          if (!response.ok) throw Error('관리 연결 코드가 없거나 만료·사용됐습니다.');
          if (epoch!==generation) {await authFetch('/v1/auth/logout',{});return;}
          cookieSession=true;
        } else if (legacy) bearer=legacy;
        else cookieSession=true;
      });
      if (epoch!==generation) return;
      await overview(requested);
    } catch (error) {
      if (epoch!==generation) return;
      reset();notice(error.message||'관리 연결을 확인하지 못했습니다.',true);
    }
  });
  $('tier-projects').addEventListener('input',()=>{
    viewRequest++;runtimeRequest++;abortReads();wipe();$('tier-runtime').replaceChildren();$('tier-runtime-read').disabled=!bearer&&!cookieSession;$('tier-refresh').disabled=!bearer&&!cookieSession;
    notice('프로젝트 선택이 바뀌었습니다. 현황을 새로 조회해 주십시오.');
  });
  $('tier-refresh').addEventListener('click',()=>{if (!bearer&&!cookieSession) return;try {overview(projects());} catch {wipe();notice('프로젝트 이름을 확인해 주십시오.',true);}});
  $('tier-next').addEventListener('click',()=>{if (!nextCursor || (!bearer&&!cookieSession)) return;try {return overview(projects(),nextCursor);} catch {wipe();notice('프로젝트 이름을 확인해 주십시오.',true);}});
  $('tier-runtime-read').addEventListener('click',async()=>{
    if (!bearer&&!cookieSession) return;
    const epoch=generation, scope=$('tier-projects').value, credential=bearer, cookie=cookieSession, serial=++runtimeRequest;
    const controller=new AbortController();pending.add(controller);const timeout=setTimeout(()=>controller.abort(),20000);
    const current=()=>epoch===generation&&serial===runtimeRequest&&scope===$('tier-projects').value&&credential===bearer&&cookie===cookieSession;
    $('tier-runtime').replaceChildren(el('p','연결·감독 기록을 조회하고 있습니다.'));
    try {
      const requested=projects(),headers={'Content-Type':'application/json','X-Taesan-Client':'dashboard'};
      if (credential) headers.Authorization='Bearer '+credential;
      const results=await Promise.allSettled(['fleet/inventory','continuous/status'].map(async route=>{
        const response=await fetch('/v1/representative/runtime/'+route,{method:'POST',headers,body:JSON.stringify({projects:requested}),credentials:cookie?'same-origin':'omit',cache:'no-store',redirect:'error',signal:controller.signal});
        if ([401,403].includes(response.status)) throw {revoked:true};
        if (!response.ok) throw Error('unavailable');
        const body=await response.json();if (!body.ok||!body.result||body.result.operational_change!==false) throw Error('unverified');return body.result;
      }));
      if (!current()) return;
      if (results.some(result=>result.status==='rejected'&&result.reason&&result.reason.revoked)) {reset();notice('관리 연결이 만료·해제됐거나 권한이 없어 화면을 비웠습니다.',true);return;}
      $('tier-runtime').replaceChildren();
      const fleet=results[0], continuous=results[1];
      if (fleet.status==='fulfilled'&&Array.isArray(fleet.value.projects)&&fleet.value.projects.length<=100) {
        for (const project of fleet.value.projects) {
          if (!requested.includes(project.project)||!Array.isArray(project.hosts)||!Array.isArray(project.sessions)||project.sessions.length>200) throw Error('scope');
          const ready=project.sessions.filter(session=>session.host_transport_observed===true).length;
          $('tier-runtime').append(el('p',safeText(project.project)+' · 등록 호스트 '+project.hosts.length+'개 · 세션 전체 '+count(project.total_session_count)+'개 기록 중 '+count(project.displayed_session_count)+'개 표시 · 표시 기록 중 연결 관측 '+ready+'개 · 생성 확인 필요 '+count(project.uncertain_creations)+'건'));
        }
      } else $('tier-runtime').append(el('p','클라우드 연결 목록은 확인하지 못했습니다.','error'));
      if (continuous.status==='fulfilled'&&Array.isArray(continuous.value.actions)&&continuous.value.actions.length<=200) {
        if (continuous.value.actions.some(action=>!requested.includes(action.project))) throw Error('scope');
        $('tier-runtime').append(el('p','감독 리더 현재 관측: '+(continuous.value.leader_observed===true?'있음':'없음')+' · 최근 처리 기록 '+continuous.value.actions.length+'건'+(continuous.value.truncated===true?' (일부 표시)':'')));
      } else $('tier-runtime').append(el('p','감독 상태는 확인하지 못했습니다.','error'));
      $('tier-runtime').append(el('p','등록·관측 기록입니다. 기존 Codex 채팅 전체 제어 및 상시 감독 지속은 이 조회만으로 입증되지 않습니다.','muted'));
    } catch {if (current()) $('tier-runtime').replaceChildren(el('p','연결·감독 상태를 확인하지 못했습니다. 자동 재시도하지 않습니다.','error'));}
    finally {clearTimeout(timeout);pending.delete(controller);}
  });
  $('tier-disconnect').addEventListener('click',async()=>{
    reset();const epoch=generation;
    try {await queuedAuth(()=>authFetch('/v1/auth/logout',{}));if (epoch===generation) notice('연결을 해제하고 현황을 비웠습니다.');}
    catch {if (epoch===generation) notice('화면은 비웠지만 서버 세션 종료는 확인이 필요합니다.',true);}
  });
})();
