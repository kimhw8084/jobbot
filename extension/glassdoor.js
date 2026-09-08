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
    return{root:null,cards:[],attempts,candidate_links_total:allLinks.length,candidate_links_outside_scope:allLinks.length};
  }
  function collect(){
    const scope=locateSearchResults();
    if(!scope.root)return{links:[],extraction_scope_missing:true,extraction_diagnostics:scope};
    const seen=new Map(),scopedAnchors=new Set();
    for(const card of scope.cards)for(const selector of S.searchLinks)for(const anchor of card.querySelectorAll(selector))scopedAnchors.add(anchor);
    for(const anchor of scopedAnchors){
      const raw=C.absoluteUrl(anchor.getAttribute('href')||anchor.href||''),id=sid(raw),url=canon(raw);if(!id||!url)continue;
      const card=anchor.closest('li,[data-test="jobListing"],[class*="JobCard_jobCard"],[class*="jobCard"]')||anchor.parentElement,txt=C.clean(card?.innerText||''),age=txt.match(/(?:^|\s)(\d+\s*(?:d\+?|day(?:s)?|h|hr|hour(?:s)?|w|wk|week(?:s)?|mo|month(?:s)?))(?:\s|$)/i),posted=C.clean(age?.[1]||card?.querySelector?.('[data-test="job-age"],[class*="listing-age"],[class*="job-age"]')?.innerText||'');
      seen.set(id,{source_job_id:id,url,title:C.clean(anchor.innerText||anchor.getAttribute('aria-label')||''),company:C.clean(card?.querySelector?.('[data-test="employer-name"],[class*="EmployerProfile_employerName"],[class*="employerName"]')?.innerText||''),location:C.clean(card?.querySelector?.('[data-test="emp-location"],[data-test="job-location"],[class*="location"]')?.innerText||''),posted_text:posted,posted_age_days:C.parseAgeDays(posted)});
    }
    const allLinks=[...new Set(S.searchLinks.flatMap((selector)=>[...document.querySelectorAll(selector)]))];
    return{links:[...seen.values()],extraction_scope_missing:false,extraction_diagnostics:{attempts:scope.attempts,matched_containers:1,candidate_links_total:allLinks.length,candidate_links_in_scope:scopedAnchors.size,candidate_links_outside_scope:Math.max(0,allLinks.length-scopedAnchors.size)}};
  }
  function nextUrl(){for(const selector of S.nextLinks){const anchor=document.querySelector(selector);if(anchor?.href)return C.absoluteUrl(anchor.href);}return '';}
  function inspectAuth(){
    const ch=C.challengeInfo(),url=location.href.toLowerCase(),body=C.clean(document.body?.innerText||'').toLowerCase(),login=/\/profile\/login|\/member\/login|signin|sign-in/.test(url),sign=S.authSignIn.some(s=>!!document.querySelector(s)),positive=body.includes('notifications')||body.includes('my jobs')||body.includes('saved jobs'),authenticated=!ch.challenged&&!login&&(positive||!sign);
    return{platform:'glassdoor',page_type:'auth',authenticated,challenged:ch.challenged,challenge_reason:ch.reason,page_url:location.href,reason:ch.challenged?ch.reason:(authenticated?'Glassdoor session authenticated':'Glassdoor sign-in required')};
  }
  function inspectSearch(){const ch=C.challengeInfo(),end=C.exhaustionInfo(['no jobs match your search']),results=collect();return{platform:'glassdoor',page_type:'search',challenged:ch.challenged,challenge_reason:ch.reason,login_required:/\/profile\/login|\/member\/login/.test(location.href.toLowerCase()),page_url:location.href,result_links:results.links,extraction_scope_missing:results.extraction_scope_missing,extraction_diagnostics:results.extraction_diagnostics,next_url:nextUrl(),exhausted:end.exhausted,exhaustion_reason:end.reason,title:document.title};}
  function inspectJob(){
    const ch=C.challengeInfo(),x=C.parseJsonLdJob()||{},title=x.title||C.firstText(S.title),company=x.company||C.firstText(S.company),locationText=x.location||C.firstText(S.location),description=x.description||C.firstText(S.description)||C.headingSectionText(['job description','about the job']),salary=x.salary_text||C.firstText(S.salary),posted=C.clean(x.posted_at||C.firstText(S.posted)),id=sid(location.href),url=canon(location.href);
    return{platform:'glassdoor',page_type:'job',challenged:ch.challenged,challenge_reason:ch.reason,page_url:location.href,job:{source_job_id:id,canonical_url:url,apply_url:x.apply_url||url,title:C.clean(title),company:C.clean(company),location:C.clean(locationText),remote_status:/remote|work from home|wfh/i.test(`${locationText} ${description.slice(0,2500)}`)?'remote':'unknown',employment_type:C.clean(x.employment_type),salary_text:C.clean(salary),posted_at:posted,posted_age_days:C.parseAgeDays(posted),valid_through:C.clean(x.valid_through),description:C.clip(description)}};
  }
  function inspect(){const p=location.pathname.toLowerCase();if(p.includes('/member/')||p.includes('/profile/login'))return inspectAuth();if(p.includes('/job-listing/'))return inspectJob();return inspectSearch();}
  function advance(){const button=[...document.querySelectorAll('button')].find(b=>!b.disabled&&/show more|next/i.test(C.clean(b.getAttribute('aria-label')||b.innerText||'')));if(button){button.click();return{advanced:true,method:'click'};}return{advanced:false};}
  async function inspectJobEventually(){let last=inspectJob();for(let i=0;i<12&&(!last.job?.title||!last.job?.description);i++){await new Promise(r=>setTimeout(r,600));last=inspectJob();}return last;}
  chrome.runtime.onMessage.addListener((m,_s,send)=>{if(m?.type==='JOBBOT_INSPECT_AUTH'){send(inspectAuth());return true;}if(m?.type==='JOBBOT_INSPECT_SEARCH'){send(inspectSearch());return true;}if(m?.type==='JOBBOT_INSPECT_DETAIL'){inspectJobEventually().then(send);return true;}if(m?.type==='JOBBOT_INSPECT'){send(inspect());return true;}if(m?.type==='JOBBOT_SCROLL_AND_INSPECT'){C.scrollResults();setTimeout(()=>send(inspectSearch()),Math.max(700,Math.min(3500,Number(m.wait_ms||1500))));return true;}if(m?.type==='JOBBOT_ADVANCE_SEARCH'){const r=advance();setTimeout(()=>send({...r,page_url:location.href}),r.advanced?1600:0);return true;}return false;});
})();
