(() => {
  'use strict';
  const C=globalThis.JobBotCommon, S=globalThis.JobBotSelectors?.linkedin;
  if(!C||!S)return;
  const sid=(url)=>{try{const u=new URL(url,location.href),m=u.pathname.match(/\/jobs\/view\/(\d+)/);return m?m[1]:C.clean(u.searchParams.get('currentJobId')||'');}catch(_){return '';}};
  const canon=(href)=>{try{const id=sid(href);return id?`https://www.linkedin.com/jobs/view/${id}/`:new URL(href,location.href).href;}catch(_){return '';}};
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
    const allLinks=[...document.querySelectorAll('a[href*="/jobs/view/"]')];
    return{root:null,cards:[],attempts,candidate_links_total:allLinks.length,candidate_links_outside_scope:allLinks.length};
  }
  function collect(){
    const scope=locateSearchResults();
    if(!scope.root)return{links:[],extraction_scope_missing:true,extraction_diagnostics:scope};
    const seen=new Map(),scopedAnchors=new Set();
    for(const card of scope.cards)for(const selector of S.searchLinks)for(const anchor of card.querySelectorAll(selector))scopedAnchors.add(anchor);
    for(const anchor of scopedAnchors){
      const raw=C.absoluteUrl(anchor.getAttribute('href')||anchor.href||''),id=sid(raw),url=canon(raw);
      if(!id||!url)continue;
      const card=scope.cards.find((candidate)=>candidate.contains(anchor))||anchor.parentElement;
      const posted=C.clean(card?.querySelector?.('time,.job-search-card__listdate,.job-card-container__listed-time,[class*="listed-time"]')?.innerText||card?.querySelector?.('time')?.getAttribute?.('datetime')||'');
      seen.set(id,{source_job_id:id,url,title:C.clean(anchor.innerText||anchor.getAttribute('aria-label')||''),company:C.clean(card?.querySelector?.('.job-card-container__primary-description,.artdeco-entity-lockup__subtitle,.base-search-card__subtitle')?.innerText||''),location:C.clean(card?.querySelector?.('.job-card-container__metadata-item,.job-search-card__location,.base-search-card__metadata')?.innerText||''),posted_text:posted,posted_age_days:C.parseAgeDays(posted)});
    }
    const allLinks=[...document.querySelectorAll('a[href*="/jobs/view/"]')];
    return{links:[...seen.values()],extraction_scope_missing:false,extraction_diagnostics:{attempts:scope.attempts,matched_containers:1,candidate_links_total:allLinks.length,candidate_links_in_scope:scopedAnchors.size,candidate_links_outside_scope:Math.max(0,allLinks.length-scopedAnchors.size)}};
  }
  function nextUrl(){
    for(const selector of S.nextLinks){const anchor=document.querySelector(selector);if(anchor?.href)return C.absoluteUrl(anchor.href);}
    try{const u=new URL(location.href),start=Number(u.searchParams.get('start')||0);if(collect().links.length>=5){u.searchParams.set('start',String(start+25));return u.href;}}catch(_){}
    return '';
  }
  function inspectAuth(){
    const ch=C.challengeInfo(),url=location.href.toLowerCase(),body=C.clean(document.body?.innerText||'').toLowerCase();
    const login=/\/login|\/checkpoint|\/authwall/.test(url),sign=S.authSignIn.some(s=>!!document.querySelector(s));
    const positive=body.includes('my jobs')||body.includes('saved jobs')||S.authPositive.some(s=>!!document.querySelector(s));
    const authenticated=!ch.challenged&&!login&&(positive||!sign);
    return{platform:'linkedin',page_type:'auth',authenticated,challenged:ch.challenged,challenge_reason:ch.reason,page_url:location.href,reason:ch.challenged?ch.reason:(authenticated?'LinkedIn session authenticated':'LinkedIn sign-in required')};
  }
  function inspectSearch(){
    const ch=C.challengeInfo(),end=C.exhaustionInfo(['no matching jobs found']),url=location.href.toLowerCase();
    const results=collect();
    return{platform:'linkedin',page_type:'search',challenged:ch.challenged,challenge_reason:ch.reason,login_required:/\/login|\/checkpoint|\/authwall/.test(url),page_url:location.href,result_links:results.links,extraction_scope_missing:results.extraction_scope_missing,extraction_diagnostics:results.extraction_diagnostics,next_url:nextUrl(),exhausted:end.exhausted,exhaustion_reason:end.reason,title:document.title};
  }
  function inspectJob(){
    const ch=C.challengeInfo(),x=C.parseJsonLdJob()||{},pageTitle=C.clean(document.title).replace(/\s*\|\s*LinkedIn\s*$/i,'').split('|')[0];
    const title=x.title||C.firstText(S.title)||pageTitle,company=x.company||C.firstText(S.company),locationText=x.location||C.firstText(S.location);
    const description=x.description||C.firstText(S.description)||C.headingSectionText(['about the job','job description']),posted=x.posted_at||C.firstText(S.posted),id=sid(location.href),url=canon(location.href);
    return{platform:'linkedin',page_type:'job',challenged:ch.challenged,challenge_reason:ch.reason,page_url:location.href,job:{source_job_id:id,canonical_url:url,apply_url:x.apply_url||url,title:C.clean(title),company:C.clean(company),location:C.clean(locationText),remote_status:/remote|work from home|wfh/i.test(`${locationText} ${description.slice(0,2500)}`)?'remote':'unknown',employment_type:C.clean(x.employment_type),salary_text:C.clean(x.salary_text),posted_at:C.clean(posted),posted_age_days:C.parseAgeDays(posted),valid_through:C.clean(x.valid_through),description:C.clip(description)}};
  }
  function inspect(){const p=location.pathname.toLowerCase();if(p.includes('/my-items/')||p.includes('/login')||p.includes('/checkpoint')||p.includes('/authwall'))return inspectAuth();if(p.includes('/jobs/view/'))return inspectJob();return inspectSearch();}
  function advance(){const button=S.nextButtons.map(s=>document.querySelector(s)).find(Boolean);if(button){button.click();return{advanced:true,method:'click'};}return{advanced:false};}
  // Give the SPA a short hydration window, but do not add several seconds to
  // every detail when the page has no usable description container. The job is
  // still persisted as failed/retryable rather than being promoted without a
  // description.
  async function inspectJobEventually(){let last=inspectJob();for(let i=0;i<6&&(!last.job?.title||!last.job?.description);i++){await new Promise(r=>setTimeout(r,450));last=inspectJob();}return last;}
  chrome.runtime.onMessage.addListener((m,_s,send)=>{if(m?.type==='JOBBOT_INSPECT_AUTH'){send(inspectAuth());return true;}if(m?.type==='JOBBOT_INSPECT_SEARCH'){send(inspectSearch());return true;}if(m?.type==='JOBBOT_INSPECT_DETAIL'){inspectJobEventually().then(send);return true;}if(m?.type==='JOBBOT_INSPECT'){send(inspect());return true;}if(m?.type==='JOBBOT_SCROLL_AND_INSPECT'){C.scrollResults();setTimeout(()=>send(inspectSearch()),Math.max(700,Math.min(3500,Number(m.wait_ms||1500))));return true;}if(m?.type==='JOBBOT_ADVANCE_SEARCH'){const r=advance();setTimeout(()=>send({...r,page_url:location.href}),r.advanced?1500:0);return true;}return false;});
})();
