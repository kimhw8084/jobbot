(() => {
  'use strict';
  const C=globalThis.JobBotCommon, S=globalThis.JobBotSelectors?.indeed;
  if(!C||!S)return;
  const sid=(url,el)=>{try{const u=new URL(url,location.href);return C.clean(u.searchParams.get('jk')||u.searchParams.get('vjk')||el?.dataset?.jk||el?.getAttribute?.('data-jk')||'');}catch(_){return C.clean(el?.dataset?.jk||'');}};
  const canon=(href)=>{try{const u=new URL(href,location.href),id=u.searchParams.get('jk')||u.searchParams.get('vjk')||'';return id?`https://www.indeed.com/viewjob?jk=${encodeURIComponent(id)}`:u.href;}catch(_){return '';}};
  const emptyState=()=>{const body=C.clean(document.body?.innerText||'').toLowerCase();const marker=(S.emptyStates||[]).some(s=>!!document.querySelector(s));const phrase=['no jobs found','no jobs match','no results found','we couldn\'t find','0 jobs','no available jobs'].find(x=>body.includes(x));return marker||phrase?String(phrase||'explicit empty-results marker'):'';};
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
    const empty=emptyState();
    return{root:null,cards:[],attempts,empty_state:!!empty,empty_state_reason:empty,candidate_links_total:allLinks.length,candidate_links_in_scope:0,candidate_links_outside_scope:allLinks.length,outside_scope_urls:allLinks.map((a)=>canon(a.getAttribute('href')||a.href||'')).filter(Boolean)};
  }
  function collect(){
    const scope=locateSearchResults();
    if(!scope.root)return{links:[],extraction_scope_missing:!scope.empty_state,extraction_diagnostics:scope};
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
    return{links:[...seen.values()],extraction_scope_missing:false,extraction_diagnostics:{extension_build:String(chrome.runtime?.getManifest?.().version_name||'unknown'),attempts:scope.attempts,matched_containers:1,candidate_links_total:allAnchors.length,candidate_links_in_scope:scopedAnchors.size,candidate_links_outside_scope:Math.max(0,allAnchors.length-scopedAnchors.size),in_scope_source_ids:[...inScopeIds],in_scope_urls:[...inScopeUrls],outside_scope_source_ids:[...outsideIds],outside_scope_urls:[...outsideUrls]}};
  }
  function nextUrl(){for(const selector of S.nextLinks){const anchor=document.querySelector(selector);if(anchor?.href)return C.absoluteUrl(anchor.href);}return '';}
  function inspectAuth(){
    const ch=C.challengeInfo(),url=location.href.toLowerCase(),body=C.clean(document.body?.innerText||'').toLowerCase(),login=/secure\.indeed\.com\/auth|\/account\/login|\/account\/register/.test(url);
    const sign=S.authSignIn.some(s=>!!document.querySelector(s)),positive=body.includes('my jobs')||body.includes('saved jobs')||S.authPositive.some(s=>!!document.querySelector(s)),authenticated=!ch.challenged&&!login&&(positive||!sign);
    return{platform:'indeed',page_type:'auth',authenticated,challenged:ch.challenged,challenge_reason:ch.reason,page_url:location.href,reason:ch.challenged?ch.reason:(authenticated?'Indeed session authenticated':'Indeed sign-in required')};
  }
  function inspectSearch(){
    const ch=C.challengeInfo(),end=C.exhaustionInfo(['no jobs matching your search']);const body=C.clean(document.body?.innerText||'').toLowerCase();
    const login=/secure\.indeed\.com\/auth|\/account\/login/.test(location.href.toLowerCase())||(S.authSignIn.some(s=>!!document.querySelector(s))&&body.includes('sign in'));
    const results=collect();
    return{platform:'indeed',extension_build:String(chrome.runtime?.getManifest?.().version_name||'unknown'),page_type:'search',challenged:ch.challenged,challenge_reason:ch.reason,login_required:login,page_url:location.href,result_links:results.links,extraction_scope_missing:results.extraction_scope_missing,extraction_diagnostics:results.extraction_diagnostics,next_url:nextUrl(),exhausted:end.exhausted||!!results.extraction_diagnostics?.empty_state,exhaustion_reason:end.reason||results.extraction_diagnostics?.empty_state_reason||'',title:document.title};
  }
  function inspectJob(){
    const ch=C.challengeInfo(),x=C.parseJsonLdJob()||{},title=x.title||C.firstText(S.title),company=x.company||C.firstText(S.company),locationText=x.location||C.firstText(S.location);
    const description=x.description||C.firstText(S.description)||C.headingSectionText(['job description','about the job']),salary=x.salary_text||C.firstText(S.salary),posted=x.posted_at||C.firstText(S.posted),id=sid(location.href,null),url=canon(location.href);
    return{platform:'indeed',page_type:'job',challenged:ch.challenged,challenge_reason:ch.reason,page_url:location.href,job:{source_job_id:id,canonical_url:url,apply_url:x.apply_url||url,title:C.clean(title),company:C.clean(company),location:C.clean(locationText),remote_status:/remote|work from home|wfh/i.test(`${locationText} ${description.slice(0,2500)}`)?'remote':'unknown',employment_type:C.clean(x.employment_type),salary_text:C.clean(salary),posted_at:C.clean(posted),posted_age_days:C.parseAgeDays(posted),valid_through:C.clean(x.valid_through),description:C.clip(description)}};
  }
  function inspect(){const p=location.pathname.toLowerCase();if(p.includes('/myjobs')||p.includes('/account/')||location.host.startsWith('secure.indeed.'))return inspectAuth();if(p.includes('/viewjob')||new URL(location.href).searchParams.has('jk')&&!p.includes('/jobs'))return inspectJob();return inspectSearch();}
  function advance(){const button=S.nextButtons.map(s=>document.querySelector(s)).find(Boolean);if(button){button.click();return{advanced:true,method:'click'};}return{advanced:false};}
  async function inspectJobEventually(options={}){let last=inspectJob(),deadline=Date.now()+Math.max(500,Number(options.hydration_timeout_ms||options.timeout_ms||5000));while(Date.now()<deadline&&!last.challenged&&(!last.job?.title||!last.job?.description)){await C.waitForDomQuiet(options.quiet_ms,options.timeout_ms);last=inspectJob();}return last;}
  chrome.runtime.onMessage.addListener((m,_s,send)=>{if(m?.type==='JOBBOT_WAIT_DOM_QUIET'){C.waitForDomQuiet(m.quiet_ms,m.timeout_ms).then(send);return true;}if(m?.type==='JOBBOT_INSPECT_AUTH'){send(inspectAuth());return true;}if(m?.type==='JOBBOT_INSPECT_SEARCH'){send(inspectSearch());return true;}if(m?.type==='JOBBOT_INSPECT_DETAIL'){inspectJobEventually(m).then(send);return true;}if(m?.type==='JOBBOT_INSPECT'){send(inspect());return true;}if(m?.type==='JOBBOT_SCROLL_AND_INSPECT'){C.scrollResults();C.waitForDomQuiet(m.quiet_ms||800,m.quiet_timeout_ms||5000).then(()=>send(inspectSearch()));return true;}if(m?.type==='JOBBOT_ADVANCE_SEARCH'){const r=advance();C.waitForDomQuiet(m.quiet_ms||800,m.quiet_timeout_ms||5000).then(()=>send({...r,page_url:location.href}));return true;}return false;});
})();
