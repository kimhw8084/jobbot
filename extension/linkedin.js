(() => {
  'use strict';
  const C=globalThis.JobBotCommon, S=globalThis.JobBotSelectors?.linkedin;
  if(!C||!S)return;
  const BUILD_ID=String(chrome.runtime?.getManifest?.().version_name||'unknown'),JOB_LINK_SELECTOR='a[href*="/jobs/view/"]',MAX_DIAGNOSTIC_ANCHORS=20,MAX_STRUCTURAL_SUMMARIES=30;
  const emptyObservations=new Map();
  const sid=(url)=>{try{const u=new URL(url,location.href),m=u.pathname.match(/\/jobs\/view\/(\d+)/);return m?m[1]:C.clean(u.searchParams.get('currentJobId')||'');}catch(_){return '';}};
  const canon=(href)=>{try{const id=sid(href);return id?`https://www.linkedin.com/jobs/view/${id}/`:new URL(href,location.href).href;}catch(_){return '';}};
  const rawHref=(anchor)=>anchor?.getAttribute?.('href')||anchor?.href||'';
  const anchorRecord=(anchor)=>{const raw=C.absoluteUrl(rawHref(anchor)),id=sid(raw),url=canon(raw);return id&&url?{anchor,id,url,search_evidence:searchEvidence(anchor)}:null;};
  const attr=(node,name)=>C.clean(node?.getAttribute?.(name)||'');
  const classTokens=(node)=>attr(node,'class').split(/\s+/).filter(Boolean);
  const stableClassTokens=(node)=>classTokens(node).filter((value)=>!/^\d+$/.test(value)&&!/^[a-f0-9]{10,}$/i.test(value)&&!/(^|[-_])\d{3,}($|[-_])/.test(value)).slice(0,6);
  const nodeSignature=(node)=>{
    if(!node?.tagName||String(node.tagName).startsWith('#'))return '';
    const tag=String(node.tagName).toLowerCase(),classes=stableClassTokens(node),attrs=[];
    for(const name of ['role','data-view-name','data-testid','data-test','data-occludable-job-id','data-job-id','aria-label']){const value=attr(node,name);if(value)attrs.push(`${name}=${value.slice(0,80)}`);}
    if(!classes.length&&!attrs.length&&tag!=='li')return '';
    return `${tag}${classes.length?'.'+classes.join('.'):''}${attrs.length?'['+attrs.join('|')+']':''}`;
  };
  const ancestorChain=(node,limit=14)=>{const chain=[];for(let current=node,i=0;current&&i<limit;current=current.parentElement,i++)chain.push(current);return chain;};
  const searchEvidence=(anchor)=>{
    const values=[rawHref(anchor),attr(anchor,'data-view-name'),attr(anchor,'data-tracking-control-name'),attr(anchor,'aria-label')];
    for(const node of ancestorChain(anchor,5))values.push(attr(node,'class'),attr(node,'role'),attr(node,'data-view-name'),attr(node,'data-testid'),attr(node,'data-occludable-job-id'));
    return /flagship3_search_srp_jobs|search[_-](?:srp|result)|jobs?[-_](?:search|result)|job[-_]?card|occludable[-_]?job[-_]?id|result[-_]?card/i.test(values.join(' '));
  };
  const allJobRecords=()=>[...document.querySelectorAll(JOB_LINK_SELECTOR)].map(anchorRecord).filter(Boolean);
  const safePageUrl=()=>{try{const u=new URL(location.href);return `${u.origin}${u.pathname}`;}catch(_){return C.clean(location.href);}};
  const rejectedRoot=(root)=>{
    const tag=String(root?.tagName||'').toLowerCase(),text=`${attr(root,'class')} ${attr(root,'role')} ${attr(root,'aria-label')} ${attr(root,'data-view-name')}`.toLowerCase();
    if(!root||['html','body','#root','#document'].includes(tag))return 'generic_document_root';
    if(tag==='main'&&!/(search|result|job|list)/i.test(text))return 'generic_main';
    if(/global[-_ ]?nav|global[-_ ]?footer|page[-_ ]?shell|detail[-_ ]?pane|recommend|similar|people[-_ ]?also|sidebar|footer|header|nav|modal|dialog|overlay/.test(text))return 'unrelated_module';
    return '';
  };
  const lowestCommonAncestor=(nodes)=>{
    if(!nodes.length)return null;
    for(const candidate of ancestorChain(nodes[0],40))if(nodes.every((node)=>candidate.contains?.(node)))return candidate;
    return null;
  };
  function structuralScope(records){
    const strong=records.filter((record)=>record.search_evidence),pool=strong.length>=2?strong:records,groups=new Map();
    for(const record of pool){
      ancestorChain(record.anchor).forEach((node,distance)=>{
        const signature=nodeSignature(node);if(!signature)return;
        const group=groups.get(signature)||{signature,entries:[]};
        group.entries.push({record,node,distance});groups.set(signature,group);
      });
    }
    const summaries=[],candidates=[];
    for(const group of groups.values()){
      const byId=new Map();
      for(const entry of group.entries){
        const ids=new Set(pool.filter((record)=>entry.node.contains?.(record.anchor)).map((record)=>record.id));
        if(ids.size!==1)continue;
        const prior=byId.get(entry.record.id);if(!prior||entry.distance<prior.distance)byId.set(entry.record.id,entry);
      }
      if(byId.size<2)continue;
      const cards=[...byId.values()].map((entry)=>entry.node),root=lowestCommonAncestor(cards),rootReason=rejectedRoot(root);
      const summary={signature:group.signature,distinct_job_ids:byId.size,card_nodes:cards.length,root_signature:nodeSignature(root),root_rejected_reason:rootReason||'',tracking_evidence:strong.filter((record)=>byId.has(record.id)).length};
      summaries.push(summary);
      if(root&&!rootReason)candidates.push({root,cards,card_signature:group.signature,summary,score:[byId.size,summary.tracking_evidence,-Math.max(...cards.map((card)=>ancestorChain(card).length))]});
    }
    candidates.sort((a,b)=>b.score[0]-a.score[0]||b.score[1]-a.score[1]||b.score[2]-a.score[2]);
    return{candidate:candidates[0]||null,strong_evidence_count:strong.length,pool_count:pool.length,summaries:summaries.slice(0,MAX_STRUCTURAL_SUMMARIES)};
  }
  const emptyState=()=>{
    if(allJobRecords().length)return '';
    const body=C.clean(document.body?.innerText||'').toLowerCase(),title=C.clean(document.title).toLowerCase(),direct=['no matching jobs found','no jobs found','no jobs match','no results found','there are no jobs','end of results'].find((phrase)=>body.includes(phrase));
    if(direct)return direct;
    // LinkedIn can advance a valid search to an offset beyond its final page
    // without rendering an explicit empty-results phrase. Treat only that
    // bounded, non-initial page as a verified end state; initial empty pages
    // remain fail-closed as extraction_scope_missing.
    try{
      const u=new URL(location.href),start=Number(u.searchParams.get('start')||0),pageText=`${title} ${body}`;
      if(start>0&&/\bjobs?\b/.test(title)&&!/(captcha|challenge|sign in|log in|error|unavailable|temporarily)/.test(pageText)){
        const key=`${location.origin}${location.pathname}?${u.searchParams.toString()}`;
        const fingerprint=`${title}|${body.slice(0,1200)}|${document.querySelectorAll('*').length}`;
        const previous=emptyObservations.get(key);
        if(previous?.fingerprint===fingerprint){emptyObservations.set(key,{fingerprint,at:previous.at,count:previous.count+1});return 'paged_empty_end_state_stable';}
        emptyObservations.set(key,{fingerprint,at:Date.now(),count:1});
      }
    }catch(_){}
    return '';
  };
  const diagnosticNode=(node)=>({signature:nodeSignature(node),tag:String(node?.tagName||'').toLowerCase(),class:attr(node,'class').slice(0,160),role:attr(node,'role').slice(0,80),data_view_name:attr(node,'data-view-name').slice(0,120),data_testid:attr(node,'data-testid').slice(0,120)});
  function missingDiagnostics(attempts,records,structural,reason){
    const inIds=[],outIds=[...new Set(records.map((record)=>record.id))];
    return{diagnostic_version:1,extension_build:BUILD_ID,page_url:safePageUrl(),title:C.clean(document.title).slice(0,200),attempts,candidate_links_total:records.length,candidate_links_in_scope:0,candidate_links_outside_scope:outIds.length,in_scope_source_ids:inIds,in_scope_urls:[],outside_scope_source_ids:outIds.slice(0,MAX_DIAGNOSTIC_ANCHORS),outside_scope_urls:[...new Set(records.map((record)=>record.url))].slice(0,MAX_DIAGNOSTIC_ANCHORS),anchor_samples:records.slice(0,MAX_DIAGNOSTIC_ANCHORS).map((record)=>({source_job_id:record.id,url:record.url,class:attr(record.anchor,'class').slice(0,160),aria_label:attr(record.anchor,'aria-label').slice(0,120),data_view_name:attr(record.anchor,'data-view-name').slice(0,120),data_tracking_control_name:attr(record.anchor,'data-tracking-control-name').slice(0,120),ancestors:ancestorChain(record.anchor,5).map(diagnosticNode)})),structural_candidate_signatures:structural.summaries,structural_strong_evidence:structural.strong_evidence_count,structural_pool_size:structural.pool_count,reason};
  }
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
    const records=allJobRecords(),structural=structuralScope(records);
    if(structural.candidate)return{root:structural.candidate.root,cards:structural.candidate.cards,attempts,scope_method:'structural',chosen_root_signature:nodeSignature(structural.candidate.root),chosen_card_signature:structural.candidate.card_signature,structural_candidate_signatures:structural.summaries,structural_strong_evidence:structural.strong_evidence_count};
    const empty=emptyState();
    return{root:null,cards:[],attempts,empty_state:!!empty,empty_state_reason:empty,scope_method:'none',...missingDiagnostics(attempts,records,structural,empty?'verified_empty_state':'no_safe_result_cluster')};
  }
  function collect(){
    const scope=locateSearchResults();
    if(!scope.root&& !scope.empty_state)return{links:[],extraction_scope_missing:true,extraction_diagnostics:scope};
    if(!scope.root&&scope.empty_state)return{links:[],extraction_scope_missing:false,extraction_diagnostics:{...scope,diagnostic_version:1,extension_build:BUILD_ID}};
    const seen=new Map(),scopedAnchors=new Set();
    for(const card of scope.cards)for(const selector of S.searchLinks)for(const anchor of card.querySelectorAll(selector))scopedAnchors.add(anchor);
    const allAnchors=[...new Set(document.querySelectorAll('a[href*="/jobs/view/"]'))];
    const inScopeIds=new Set(),inScopeUrls=new Set(),outsideIds=new Set(),outsideUrls=new Set();
    for(const anchor of scopedAnchors){
      const raw=C.absoluteUrl(rawHref(anchor)),id=sid(raw),url=canon(raw);
      if(!id||!url)continue;
      inScopeIds.add(id);inScopeUrls.add(url);
      const card=scope.cards.find((candidate)=>candidate.contains(anchor))||anchor.parentElement;
      const posted=C.clean(card?.querySelector?.('time,.job-search-card__listdate,.job-card-container__listed-time,[class*="listed-time"]')?.innerText||card?.querySelector?.('time')?.getAttribute?.('datetime')||'');
      seen.set(id,{source_job_id:id,url,title:C.clean(anchor.innerText||anchor.getAttribute('aria-label')||''),company:C.clean(card?.querySelector?.('.job-card-container__primary-description,.artdeco-entity-lockup__subtitle,.base-search-card__subtitle')?.innerText||''),location:C.clean(card?.querySelector?.('.job-card-container__metadata-item,.job-search-card__location,.base-search-card__metadata')?.innerText||''),posted_text:posted,posted_age_days:C.parseAgeDays(posted)});
    }
    for(const anchor of allAnchors){
      if(scopedAnchors.has(anchor))continue;
      const raw=C.absoluteUrl(rawHref(anchor)),id=sid(raw),url=canon(raw);if(!id||!url)continue;
      if(!inScopeIds.has(id))outsideIds.add(id);if(!inScopeUrls.has(url))outsideUrls.add(url);
    }
    return{links:[...seen.values()],extraction_scope_missing:false,extraction_diagnostics:{diagnostic_version:1,extension_build:BUILD_ID,attempts:scope.attempts,matched_containers:1,scope_method:scope.scope_method||'selector',chosen_root_signature:scope.chosen_root_signature||nodeSignature(scope.root),chosen_card_signature:scope.chosen_card_signature||'',structural_candidate_signatures:scope.structural_candidate_signatures||[],candidate_links_total:allAnchors.length,candidate_links_in_scope:inScopeIds.size,candidate_links_outside_scope:outsideIds.size,in_scope_source_ids:[...inScopeIds],in_scope_urls:[...inScopeUrls],outside_scope_source_ids:[...outsideIds],outside_scope_urls:[...outsideUrls]}};
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
    const verifiedEmptyReason=results.extraction_diagnostics?.empty_state_reason||'';
    return{platform:'linkedin',extension_build:BUILD_ID,page_type:'search',challenged:ch.challenged,challenge_reason:ch.reason,login_required:/\/login|\/checkpoint|\/authwall/.test(url),page_url:location.href,result_links:results.links,extraction_scope_missing:results.extraction_scope_missing,extraction_diagnostics:results.extraction_diagnostics,next_url:nextUrl(),exhausted:end.exhausted||!!verifiedEmptyReason,exhaustion_reason:end.reason||verifiedEmptyReason,title:document.title};
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
  async function inspectJobEventually(options={}){let last=inspectJob(),deadline=Date.now()+Math.max(500,Number(options.hydration_timeout_ms||options.timeout_ms||5000));while(Date.now()<deadline&&!last.challenged&&(!last.job?.title||!last.job?.description)){await C.waitForDomQuiet(options.quiet_ms,options.timeout_ms);last=inspectJob();}return last;}
  chrome.runtime.onMessage.addListener((m,_s,send)=>{if(m?.type==='JOBBOT_WAIT_DOM_QUIET'){C.waitForDomQuiet(m.quiet_ms,m.timeout_ms).then(send);return true;}if(m?.type==='JOBBOT_INSPECT_AUTH'){send(inspectAuth());return true;}if(m?.type==='JOBBOT_INSPECT_SEARCH'){send(inspectSearch());return true;}if(m?.type==='JOBBOT_INSPECT_DETAIL'){inspectJobEventually(m).then(send);return true;}if(m?.type==='JOBBOT_INSPECT'){send(inspect());return true;}if(m?.type==='JOBBOT_SCROLL_AND_INSPECT'){C.scrollResults();setTimeout(()=>C.waitForDomQuiet(m.quiet_ms||800,m.quiet_timeout_ms||5000).then(()=>send(inspectSearch())),Math.max(0,Number(m.wait_ms)||0));return true;}if(m?.type==='JOBBOT_ADVANCE_SEARCH'){const r=advance();C.waitForDomQuiet(m.quiet_ms||800,m.quiet_timeout_ms||5000).then(()=>send({...r,page_url:location.href}));return true;}return false;});
})();
