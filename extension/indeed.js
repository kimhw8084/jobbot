(() => {
  'use strict';
  const C=globalThis.JobBotCommon, S=globalThis.JobBotSelectors?.indeed;
  if(!C||!S)return;
  const sid=(url,el)=>{try{const u=new URL(url,location.href);return C.clean(u.searchParams.get('jk')||u.searchParams.get('vjk')||el?.dataset?.jk||el?.getAttribute?.('data-jk')||'');}catch(_){return C.clean(el?.dataset?.jk||'');}};
  const canon=(href)=>{try{const u=new URL(href,location.href),id=u.searchParams.get('jk')||u.searchParams.get('vjk')||'';return id?`https://www.indeed.com/viewjob?jk=${encodeURIComponent(id)}`:u.href;}catch(_){return '';}};
  function locateSearchResults(){
    const attempts=[];
    for(const selector of S.searchContainers||[]){
      const roots=[...document.querySelectorAll(selector)];
      attempts.push({selector,matched:roots.length});
      for(const root of roots){
        const cards=[];
        for(const cardSelector of S.resultCards||[])for(const card of root.querySelectorAll(cardSelector))if(!cards.includes(card))cards.push(card);
        if(!cards.length&&root.matches?.(S.resultCards?.join(',')))cards.push(root);
        if(cards.length)return{root,cards,attempts};
      }
    }
    const allLinks=[...new Set(S.searchLinks.flatMap((selector)=>[...document.querySelectorAll(selector)]))];
    const empty=C.exhaustionInfo(['no jobs matching your search','no jobs found']);
    return{root:null,cards:[],attempts,empty_state:!!empty.exhausted,empty_state_reason:empty.reason,candidate_links_total:allLinks.length,candidate_links_in_scope:0,candidate_links_outside_scope:allLinks.length,outside_scope_urls:allLinks.map((a)=>canon(a.getAttribute('href')||a.href||'')).filter(Boolean)};
  }
  function collect(){
    const scope=locateSearchResults();
    if(!scope.root&&!scope.empty_state)return{links:[],extraction_scope_missing:true,extraction_diagnostics:scope};
    if(!scope.root&&scope.empty_state)return{links:[],extraction_scope_missing:false,extraction_diagnostics:scope};
    const seen=new Map(),scopedAnchors=new Set();
    for(const card of scope.cards)for(const selector of S.searchLinks)for(const anchor of card.querySelectorAll(selector))scopedAnchors.add(anchor);
    const allAnchors=[...new Set(S.searchLinks.flatMap((selector)=>[...document.querySelectorAll(selector)]))];
    const inScopeIds=new Set(),inScopeUrls=new Set(),outsideIds=new Set(),outsideUrls=new Set();
    for(const anchor of scopedAnchors){
      const raw=C.absoluteUrl(anchor.getAttribute('href')||anchor.href||''),id=sid(raw,anchor),url=canon(raw);if(!id||!url)continue;
      inScopeIds.add(id);inScopeUrls.add(url);
      const card=anchor.closest('[data-jk],.job_seen_beacon,.result,.cardOutline,li')||anchor.parentElement,posted=C.clean(card?.querySelector?.('[data-testid="myJobsStateDate"],.date,[data-testid="job-age"]')?.innerText||'');
      seen.set(id,{source_job_id:id,url,title:C.clean(anchor.getAttribute('aria-label')||anchor.title||anchor.innerText||card?.querySelector?.('h2')?.innerText||''),company:C.clean(card?.querySelector?.('[data-testid="company-name"],.companyName,[data-testid="companyName"]')?.innerText||''),location:C.clean(card?.querySelector?.('[data-testid="text-location"],.companyLocation,[data-testid="job-location"]')?.innerText||''),posted_text:posted,posted_age_days:C.parseAgeDays(posted)});
    }
    for(const anchor of allAnchors){
      if(scopedAnchors.has(anchor))continue;
      const raw=C.absoluteUrl(anchor.getAttribute('href')||anchor.href||''),id=sid(raw,anchor),url=canon(raw);if(!id||!url)continue;
      if(!inScopeIds.has(id))outsideIds.add(id);if(!inScopeUrls.has(url))outsideUrls.add(url);
    }
    return{links:[...seen.values()],extraction_scope_missing:false,extraction_diagnostics:{attempts:scope.attempts,matched_containers:1,candidate_links_total:allAnchors.length,candidate_links_in_scope:scopedAnchors.size,candidate_links_outside_scope:Math.max(0,allAnchors.length-scopedAnchors.size),in_scope_source_ids:[...inScopeIds],in_scope_urls:[...inScopeUrls],outside_scope_source_ids:[...outsideIds],outside_scope_urls:[...outsideUrls]}};
  }
  function nextUrl(){for(const selector of S.nextLinks){const anchor=document.querySelector(selector);if(anchor?.href)return C.absoluteUrl(anchor.href);}return '';}
  function inspectAuth(){
    const ch=C.challengeInfo(),wall=C.authWallInfo(S.authSignIn,/secure\.indeed\.com\/auth|\/account\/login|\/account\/register/i),positive=C.positiveAuthInfo(S.authPositive,['my jobs','saved jobs','notifications']);
    const auth_state=ch.challenged?'challenged_cooldown':wall.required?'sign_in_required':positive.positive?'verified':'unknown';
    const authenticated=auth_state==='verified';
    return{platform:'indeed',page_type:'auth',authenticated,auth_state,login_required:auth_state==='sign_in_required',auth_evidence:positive.reason||wall.reason||'no conclusive account evidence',challenged:ch.challenged,challenge_reason:ch.reason,page_url:location.href,reason:ch.challenged?ch.reason:(wall.required?wall.reason:(authenticated?'Indeed session authenticated':'Indeed auth state unverified'))};
  }
  function inspectSearch(){
    const ch=C.challengeInfo(),end=C.exhaustionInfo(['no jobs matching your search']);
    const wall=C.authWallInfo(S.authSignIn,/secure\.indeed\.com\/auth|\/account\/login|\/account\/register/i);
    const results=collect();
    const expectedRoute=/\/jobs(?:\/|$)/.test(new URL(location.href).pathname.toLowerCase())&&new URL(location.href).searchParams.has('q');
    const verifiedEmptyReason=results.extraction_diagnostics?.empty_state_reason||'';
    const ready=!ch.challenged&&!wall.required&&expectedRoute&&!results.extraction_scope_missing&&(results.links.length>0||!!verifiedEmptyReason);
    const auth_state=ch.challenged?'challenged_cooldown':wall.required?'sign_in_required':ready?'verified':'unknown';
    return{platform:'indeed',page_type:'search',authenticated:ready,auth_state,readiness_state:ch.challenged?'challenged_cooldown':wall.required?'sign_in_required':ready?'ready':'unknown',ready,challenged:ch.challenged,challenge_reason:ch.reason,login_required:wall.required,page_url:location.href,result_links:results.links,extraction_scope_missing:results.extraction_scope_missing,extraction_diagnostics:results.extraction_diagnostics,next_url:nextUrl(),exhausted:end.exhausted||!!verifiedEmptyReason,exhaustion_reason:end.reason||verifiedEmptyReason,title:document.title};
  }
  function inspectJob(){
    const ch=C.challengeInfo(),surface=C.pageSurface(); if(surface.surface!=='job')return{platform:'indeed',page_type:surface.surface,challenged:surface.surface==='challenge',challenge_reason:surface.reason,page_url:location.href,surface_reason:surface.reason,job:null};
    const x=C.parseJsonLdJob()||{},title=x.title||C.firstText(S.title),company=x.company||C.firstText(S.company),locationText=x.location||C.firstText(S.location);
    const description=x.description||C.firstText(S.description)||C.headingSectionText(['job description','about the job']),salary=x.salary_text||C.firstText(S.salary),posted=x.posted_at||C.firstText(S.posted),id=sid(location.href,null),url=canon(location.href);
    return{platform:'indeed',page_type:'job',surface:'job',challenged:ch.challenged,challenge_reason:ch.reason,page_url:location.href,job:{source_job_id:id,canonical_url:url,apply_url:x.apply_url||'',title:C.normalizeTitle(title),company:C.clean(company),location:C.clean(locationText),remote_status:/remote|work from home|wfh/i.test(`${locationText} ${description.slice(0,2500)}`)?'remote':'unknown',employment_type:C.clean(x.employment_type),salary_text:C.clean(salary),posted_at:C.clean(posted),posted_age_days:C.parseAgeDays(posted),valid_through:C.clean(x.valid_through),description:C.clip(description)}};
  }
  const firstIn=(root,selectors)=>{for(const selector of selectors||[]){const node=root?.querySelector?.(selector);const value=C.clean(node?.innerText||node?.textContent||'');if(value)return value;}return '';};
  function paneRoot(){for(const selector of S.paneRoots||[])try{const root=document.querySelector(selector);if(root)return root;}catch(_){}return null;}
  function paneSelection(sourceId,select=true){
    const scope=locateSearchResults(),target=String(sourceId||'');
    const entry=[...scope.cards].flatMap(card=>[...card.querySelectorAll((S.searchLinks||[]).join(','))].map(node=>({card,node}))).find(x=>sid(C.absoluteUrl(x.node.getAttribute('href')||x.node.href||''),x.node)===target);
    if(!entry)return{selected:false,selection_attempted:false,identity_status:'MISSING_CARD',page_type:'search',selected_source_job_id:'',search_pane_diagnostics:{reason:'selected card disappeared'}};
    const expectedTitle=C.normalizeTitle(C.clean(entry.node.getAttribute('aria-label')||entry.node.innerText||entry.card.innerText||'').replace(/\s+with verification$/i,''));let clicked=false;
    if(select)try{entry.node.click();clicked=true;}catch(_){}
    return{selected:clicked||!select,selection_attempted:select,selected_source_job_id:target,selected_title:expectedTitle,card_metadata:entry.card.innerText?.slice(0,500)||''};
  }
  async function inspectSearchPane(sourceId,select=true){
    const searchPath=/\/jobs(?:\/|$)/.test(new URL(location.href).pathname.toLowerCase());
    const selection=paneSelection(sourceId,select);if(!selection.selected)return selection;
    for(let i=0;i<14;i++){
      const ch=C.challengeInfo();if(ch.challenged)return{...selection,page_type:'challenge',challenged:true,challenge_reason:ch.reason};
      const wall=C.authWallInfo(S.authSignIn,/secure\.indeed\.com\/auth|\/account\/login|\/account\/register/i);if(wall.required)return{...selection,page_type:'login',login_required:true,surface_reason:wall.reason};
      if(!searchPath||!/\/jobs(?:\/|$)/.test(new URL(location.href).pathname.toLowerCase()))return{...selection,page_type:'error',navigation_context_lost:true,surface_reason:'search context lost after card selection',page_url:location.href};
      const root=paneRoot(),title=C.normalizeTitle(firstIn(root,S.paneTitle||S.title)),company=firstIn(root,S.company),locationText=firstIn(root,S.location),description=firstIn(root,S.paneDescription||S.description);
      const paneId=sid(C.absoluteUrl(root?.querySelector?.('a[href*="viewjob"]')?.href||''),root);
      const identityProven=!!title&&title===selection.selected_title&&(!paneId||paneId===String(sourceId));
      if(root&&title&&description){
        return{...selection,platform:'indeed',page_type:'job',surface:'embedded_search_pane',search_pane:true,page_url:location.href,selected_source_job_id:paneId||String(sourceId),identity_status:identityProven?'PROVEN':'MISMATCH',identity_proven:identityProven,acquisition_mode:'search_pane',detail_acquisition:{mode:'search_pane',surface:'embedded_search_pane',url:location.href},job:{source_job_id:paneId||String(sourceId),canonical_url:canon(root?.querySelector?.('a[href*="viewjob"]')?.href||location.href),title,company,location:locationText,remote_status:/remote|work from home|wfh/i.test(`${locationText} ${description.slice(0,2500)}`)?'remote':'unknown',employment_type:'',salary_text:'',posted_at:'',description:C.clip(description)}};
      }
      await new Promise(r=>setTimeout(r,300));
    }
    return{...selection,platform:'indeed',page_type:'search',identity_status:'INCOMPLETE',identity_proven:false,acquisition_mode:'search_pane',search_pane_diagnostics:{reason:'pane hydration timeout'}};
  }
  function inspect(){const p=location.pathname.toLowerCase();if(p.includes('/myjobs')||p.includes('/account/')||location.host.startsWith('secure.indeed.'))return inspectAuth();if(p.includes('/viewjob')||new URL(location.href).searchParams.has('jk')&&!p.includes('/jobs'))return inspectJob();return inspectSearch();}
  function advance(){const button=S.nextButtons.map(s=>document.querySelector(s)).find(Boolean);if(button){button.click();return{advanced:true,method:'click'};}return{advanced:false};}
  async function inspectJobEventually(){let last=inspectJob();for(let i=0;i<10&&last.page_type==='job'&&(!last.job?.title||!last.job?.description);i++){await new Promise(r=>setTimeout(r,500));last=inspectJob();}return last;}
  chrome.runtime.onMessage.addListener((m,_s,send)=>{if(m?.type==='JOBBOT_RECEIVER_READY'){send(C.receiverReady('indeed'));return true;}if(m?.type==='JOBBOT_INSPECT_AUTH'){send(inspectAuth());return true;}if(m?.type==='JOBBOT_INSPECT_SEARCH'||m?.type==='JOBBOT_INSPECT_SEARCH_EVENTUALLY'){send(inspectSearch());return true;}if(m?.type==='JOBBOT_INSPECT_SEARCH_PANE'){inspectSearchPane(m.source_job_id,m.select!==false).then(send);return true;}if(m?.type==='JOBBOT_INSPECT_DETAIL'){inspectJobEventually().then(send);return true;}if(m?.type==='JOBBOT_INSPECT'){send(inspect());return true;}if(m?.type==='JOBBOT_SCROLL_AND_INSPECT'){C.scrollResults();setTimeout(()=>send(inspectSearch()),Math.max(500,Math.min(3000,Number(m.wait_ms||1200))));return true;}if(m?.type==='JOBBOT_ADVANCE_SEARCH'){const r=advance();setTimeout(()=>send({...r,page_url:location.href}),r.advanced?1200:0);return true;}return false;});
})();
