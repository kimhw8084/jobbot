(() => {
  'use strict';
  const C=globalThis.JobBotCommon, S=globalThis.JobBotSelectors?.glassdoor;
  if(!C||!S)return;
  const sid=(url)=>{try{const u=new URL(url,location.href),jl=u.searchParams.get('jl');if(jl)return jl;const m=u.pathname.match(/\/job-listing\/(.+?)-JV_/i);return m?m[1].slice(-80):u.pathname;}catch(_){return '';}};
  const canon=(href)=>{try{const u=new URL(href,location.href);u.hash='';return u.href;}catch(_){return '';}};
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
    const empty=C.exhaustionInfo(['no jobs match your search','no jobs found']);
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
      const raw=C.absoluteUrl(anchor.getAttribute('href')||anchor.href||''),id=sid(raw),url=canon(raw);if(!id||!url)continue;
      inScopeIds.add(id);inScopeUrls.add(url);
      const card=anchor.closest('li,[data-test="jobListing"],[class*="JobCard_jobCard"],[class*="jobCard"]')||anchor.parentElement,txt=C.clean(card?.innerText||''),age=txt.match(/(?:^|\s)(\d+\s*(?:d\+?|day(?:s)?|h|hr|hour(?:s)?|w|wk|week(?:s)?|mo|month(?:s)?))(?:\s|$)/i),posted=C.clean(age?.[1]||card?.querySelector?.('[data-test="job-age"],[class*="listing-age"],[class*="job-age"]')?.innerText||'');
      seen.set(id,{source_job_id:id,url,title:C.clean(anchor.innerText||anchor.getAttribute('aria-label')||''),company:C.clean(card?.querySelector?.('[data-test="employer-name"],[class*="EmployerProfile_employerName"],[class*="employerName"]')?.innerText||''),location:C.clean(card?.querySelector?.('[data-test="emp-location"],[data-test="job-location"],[class*="location"]')?.innerText||''),posted_text:posted,posted_age_days:C.parseAgeDays(posted)});
    }
    for(const anchor of allAnchors){
      if(scopedAnchors.has(anchor))continue;
      const raw=C.absoluteUrl(anchor.getAttribute('href')||anchor.href||''),id=sid(raw),url=canon(raw);if(!id||!url)continue;
      if(!inScopeIds.has(id))outsideIds.add(id);if(!inScopeUrls.has(url))outsideUrls.add(url);
    }
    return{links:[...seen.values()],extraction_scope_missing:false,extraction_diagnostics:{attempts:scope.attempts,matched_containers:1,candidate_links_total:allAnchors.length,candidate_links_in_scope:scopedAnchors.size,candidate_links_outside_scope:Math.max(0,allAnchors.length-scopedAnchors.size),in_scope_source_ids:[...inScopeIds],in_scope_urls:[...inScopeUrls],outside_scope_source_ids:[...outsideIds],outside_scope_urls:[...outsideUrls]}};
  }
  function nextUrl(){for(const selector of S.nextLinks){const anchor=document.querySelector(selector);if(anchor?.href)return C.absoluteUrl(anchor.href);}return '';}
  function inspectAuth(){
    const ch=C.challengeInfo(),wall=C.authWallInfo(S.authSignIn,/\/profile\/login|\/member\/login|signin|sign-in/i),positive=C.positiveAuthInfo(S.authPositive,['notifications','my jobs','saved jobs']);
    const auth_state=ch.challenged?'challenged_cooldown':wall.required?'sign_in_required':positive.positive?'verified':'unknown';
    const authenticated=auth_state==='verified';
    return{platform:'glassdoor',page_type:'auth',authenticated,auth_state,login_required:auth_state==='sign_in_required',auth_evidence:positive.reason||wall.reason||'no conclusive account evidence',challenged:ch.challenged,challenge_reason:ch.reason,page_url:location.href,reason:ch.challenged?ch.reason:(wall.required?wall.reason:(authenticated?'Glassdoor session authenticated':'Glassdoor auth state unverified'))};
  }
  function inspectSearch(){const ch=C.challengeInfo(),end=C.exhaustionInfo(['no jobs match your search']),wall=C.authWallInfo(S.authSignIn,/\/profile\/login|\/member\/login|signin|sign-in/i),results=collect();const expectedRoute=/\/Job\//i.test(new URL(location.href).pathname);const verifiedEmptyReason=results.extraction_diagnostics?.empty_state_reason||'';const ready=!ch.challenged&&!wall.required&&expectedRoute&&!results.extraction_scope_missing&&(results.links.length>0||!!verifiedEmptyReason);const auth_state=ch.challenged?'challenged_cooldown':wall.required?'sign_in_required':ready?'verified':'unknown';return{platform:'glassdoor',page_type:'search',authenticated:ready,auth_state,readiness_state:ch.challenged?'challenged_cooldown':wall.required?'sign_in_required':ready?'ready':'unknown',ready,challenged:ch.challenged,challenge_reason:ch.reason,login_required:wall.required,page_url:location.href,result_links:results.links,extraction_scope_missing:results.extraction_scope_missing,extraction_diagnostics:results.extraction_diagnostics,next_url:nextUrl(),exhausted:end.exhausted||!!verifiedEmptyReason,exhaustion_reason:end.reason||verifiedEmptyReason,title:document.title};}
  function inspectJob(){
    const ch=C.challengeInfo(),surface=C.pageSurface(); if(surface.surface!=='job')return{platform:'glassdoor',page_type:surface.surface,challenged:surface.surface==='challenge',challenge_reason:surface.reason,page_url:location.href,surface_reason:surface.reason,job:null};
    const x=C.parseJsonLdJob()||{},title=x.title||C.firstText(S.title),company=x.company||C.firstText(S.company),locationText=x.location||C.firstText(S.location),description=x.description||C.firstText(S.description)||C.headingSectionText(['job description','about the job']),salary=x.salary_text||C.firstText(S.salary),posted=C.clean(x.posted_at||C.firstText(S.posted)),id=sid(location.href),url=canon(location.href);
    return{platform:'glassdoor',page_type:'job',surface:'job',challenged:ch.challenged,challenge_reason:ch.reason,page_url:location.href,job:{source_job_id:id,canonical_url:url,apply_url:x.apply_url||'',title:C.normalizeTitle(title),company:C.clean(company),location:C.clean(locationText),remote_status:/remote|work from home|wfh/i.test(`${locationText} ${description.slice(0,2500)}`)?'remote':'unknown',employment_type:C.clean(x.employment_type),salary_text:C.clean(salary),posted_at:posted,posted_age_days:C.parseAgeDays(posted),valid_through:C.clean(x.valid_through),description:C.clip(description)}};
  }
  function inspect(){const p=location.pathname.toLowerCase();if(p.includes('/member/')||p.includes('/profile/login'))return inspectAuth();if(p.includes('/job-listing/'))return inspectJob();return inspectSearch();}
  function advance(){const button=[...document.querySelectorAll('button')].find(b=>!b.disabled&&/show more|next/i.test(C.clean(b.getAttribute('aria-label')||b.innerText||'')));if(button){button.click();return{advanced:true,method:'click'};}return{advanced:false};}
  async function inspectJobEventually(){let last=inspectJob();for(let i=0;i<12&&last.page_type==='job'&&(!last.job?.title||!last.job?.description);i++){await new Promise(r=>setTimeout(r,600));last=inspectJob();}return last;}
  chrome.runtime.onMessage.addListener((m,_s,send)=>{if(m?.type==='JOBBOT_INSPECT_AUTH'){send(inspectAuth());return true;}if(m?.type==='JOBBOT_INSPECT_SEARCH'||m?.type==='JOBBOT_INSPECT_SEARCH_EVENTUALLY'){send(inspectSearch());return true;}if(m?.type==='JOBBOT_INSPECT_DETAIL'){inspectJobEventually().then(send);return true;}if(m?.type==='JOBBOT_INSPECT'){send(inspect());return true;}if(m?.type==='JOBBOT_SCROLL_AND_INSPECT'){C.scrollResults();setTimeout(()=>send(inspectSearch()),Math.max(700,Math.min(3500,Number(m.wait_ms||1500))));return true;}if(m?.type==='JOBBOT_ADVANCE_SEARCH'){const r=advance();setTimeout(()=>send({...r,page_url:location.href}),r.advanced?1600:0);return true;}return false;});
})();
