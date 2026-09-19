(() => {
  'use strict';
  const C=globalThis.JobBotCommon, S=globalThis.JobBotSelectors?.linkedin;
  if(!C||!S)return;
  const BUILD_ID=String(chrome.runtime?.getManifest?.().version_name||'unknown'),JOB_LINK_SELECTOR='a[href*="/jobs/view/"]',MAX_DIAGNOSTIC_ANCHORS=20,MAX_STRUCTURAL_SUMMARIES=30,MAX_DETAIL_DIAGNOSTICS=12;
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
  const SAFE_SEARCH_PARAMS=new Set(['keywords','location','f_TPR','f_WT','start','geoId','distance','sortBy','f_JT','f_E','f_C','f_I','f_PP','f_AL','f_TS','f_VJ','f_T','origin','refresh']);
  const safePageUrl=()=>{try{const u=new URL(location.href),params=new URLSearchParams();for(const [key,value] of u.searchParams.entries())if(SAFE_SEARCH_PARAMS.has(key))params.append(key,value);const query=params.toString();return `${u.origin}${u.pathname}${query?`?${query}`:''}`;}catch(_){return C.clean(location.href);}};
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
      if(start>0&&/\bjobs?\b/.test(title)&&!/(captcha|challenge|sign in|log in|error|unavailable|temporarily)/.test(pageText))return 'paged_empty_end_state';
    }catch(_){}
    return '';
  };
  const diagnosticNode=(node)=>({signature:nodeSignature(node),tag:String(node?.tagName||'').toLowerCase(),class:attr(node,'class').slice(0,160),role:attr(node,'role').slice(0,80),data_view_name:attr(node,'data-view-name').slice(0,120),data_testid:attr(node,'data-testid').slice(0,120)});
  const diagnosticText=(node,limit=160)=>C.clip(C.textOf(node),limit);
  const diagnosticSelectorStats=(selectors,root=document)=>[...(selectors||[])].map(selector=>{
    let nodes=[];try{nodes=[...root.querySelectorAll(selector)];}catch(_){nodes=[];}
    return{selector,matched:nodes.length,text_lengths:nodes.slice(0,MAX_DETAIL_DIAGNOSTICS).map(node=>C.textOf(node).length)};
  });
  const diagnosticLabels=()=>[...document.querySelectorAll('h1,h2,h3,[role="heading"],[aria-label]')].filter(node=>/about the job|job description/i.test(`${C.textOf(node)} ${attr(node,'aria-label')}`)).slice(0,MAX_DETAIL_DIAGNOSTICS).map(node=>({text:diagnosticText(node,120),text_length:C.textOf(node).length,signature:nodeSignature(node),ancestors:ancestorChain(node,5).map(diagnosticNode)}));
  const diagnosticDescriptionCandidates=()=>{
    const nodes=[],seen=new Set();
    for(const selector of S.description||[])for(const node of document.querySelectorAll(selector))if(!seen.has(node)){seen.add(node);nodes.push(node);}
    for(const heading of [...document.querySelectorAll('h1,h2,h3,[role="heading"]')].filter(node=>/about the job|job description/i.test(C.textOf(node))).slice(0,MAX_DETAIL_DIAGNOSTICS)){
      let node=heading;
      for(let i=0;i<6&&node;i++){node=node.parentElement;if(!node||seen.has(node))continue;const length=C.textOf(node).length;if(length>=180&&length<=60000){seen.add(node);nodes.push(node);break;}}
    }
    return nodes.slice(0,MAX_DETAIL_DIAGNOSTICS).map(node=>({signature:nodeSignature(node),text_length:C.textOf(node).length,ancestors:ancestorChain(node,6).map(diagnosticNode)}));
  };
  const diagnosticJsonLd=()=>{
    const scripts=[...document.querySelectorAll('script[type="application/ld+json"]')],fields={title:0,company:0,location:0,description:0,datePosted:0};let present=false;
    const visit=(value)=>{if(!value||typeof value!=='object')return;const type=Array.isArray(value['@type'])?value['@type'].join(' '):String(value['@type']||'');if(/jobposting/i.test(type)){present=true;const org=value.hiringOrganization,loc=value.jobLocation;fields.title=C.clean(value.title).length;fields.company=C.clean(typeof org==='string'?org:org?.name).length;fields.location=C.clean(loc?JSON.stringify(loc):value.jobLocationType).length;fields.description=C.clean(value.description).length;fields.datePosted=C.clean(value.datePosted).length;}if(Array.isArray(value['@graph']))value['@graph'].forEach(visit);};
    for(const script of scripts){try{const parsed=JSON.parse(script.textContent||'{}');(Array.isArray(parsed)?parsed:[parsed]).forEach(visit);}catch(_){}}
    return{script_count:scripts.length,script_lengths:scripts.slice(0,MAX_DETAIL_DIAGNOSTICS).map(script=>(script.textContent||'').length),jobposting_present:present,field_lengths:fields};
  };
  const safeCanonicalLink=()=>{const node=document.querySelector('link[rel="canonical"]');return C.clean(node?.getAttribute?.('href')||node?.href||'').slice(0,500);};
  const cardMetadataDiagnostics=(card,anchor)=>{
    if(!card)return{card_present:false};
    const related=[];for(const node of card.querySelectorAll('*')){const attrs=`${attr(node,'class')} ${attr(node,'data-testid')} ${attr(node,'data-view-name')} ${attr(node,'aria-label')}`;if(!/(company|employer|location|posted|listed|metadata|date|time)/i.test(attrs))continue;related.push({signature:nodeSignature(node),text_length:C.textOf(node).length,text:diagnosticText(node,120)});if(related.length>=MAX_DETAIL_DIAGNOSTICS)break;}
    const descendants=[anchor,...anchor.querySelectorAll('*')].slice(0,MAX_DETAIL_DIAGNOSTICS).map(node=>({signature:nodeSignature(node),text_length:C.textOf(node).length,text:diagnosticText(node,160),aria_label:attr(node,'aria-label').slice(0,160),title:attr(node,'title').slice(0,160),data_testid:attr(node,'data-testid').slice(0,120),data_view_name:attr(node,'data-view-name').slice(0,120)}));
    return{card_present:true,card_signature:nodeSignature(card),card_text_length:C.textOf(card).length,anchor_signature:nodeSignature(anchor),anchor_text_length:C.textOf(anchor).length,anchor_text:diagnosticText(anchor,220),anchor_aria_label:attr(anchor,'aria-label').slice(0,220),anchor_title:attr(anchor,'title').slice(0,220),title_descendants:descendants,metadata_candidates:related};
  };
  const cardTitle=(anchor)=>{
    const candidates=[...anchor.querySelectorAll('span,div')].filter(node=>{
      const text=C.textOf(node),classes=attr(node,'class').toLowerCase(),hidden=attr(node,'aria-hidden').toLowerCase()==='true';
      return !!text&&!hidden&&!/visually-hidden|screen-reader|sr-only/.test(classes)&&!/^svg$/i.test(String(node.tagName||''))&&!node.closest?.('svg');
    });
    const visible=candidates.map(node=>C.clean(C.textOf(node))).filter(Boolean);
    const repeated=visible.find((text,index)=>visible.indexOf(text)!==index);
    if(repeated||visible[0])return C.normalizeTitle(repeated||visible[0]);
    const verified=anchor.querySelector('svg.text-view-model__verified-icon,svg[class*="verified"]');
    const hiddenLabel=[...anchor.querySelectorAll('span.visually-hidden,[class*="visually-hidden"]')].map(node=>C.clean(C.textOf(node))).find(Boolean);
    const ariaLabel=C.clean(anchor.getAttribute('aria-label')||'');
    if(verified&&hiddenLabel&&ariaLabel===hiddenLabel&&/\s+with verification$/i.test(hiddenLabel))return C.normalizeTitle(hiddenLabel.replace(/\s+with verification$/i,''));
    return C.normalizeTitle(ariaLabel||C.clean(anchor.innerText||''));
  };
  const detailDiagnostics=(route,sourceId,descriptionSource='none',surface=null)=>({diagnostic_version:2,extension_build:BUILD_ID,route,source_job_id:C.clean(sourceId),document_url:C.clean(location.href).slice(0,1000),document_path:C.clean(location.pathname).slice(0,300),document_title:C.clean(document.title).slice(0,240),ready_state:C.clean(document.readyState||'unknown'),body_text_length:C.clean(document.body?.innerText||'').length,page_surface:surface||C.pageSurface(),canonical_link_href:safeCanonicalLink(),selector_stats:{title:diagnosticSelectorStats(S.title),company:diagnosticSelectorStats(S.company),location:diagnosticSelectorStats(S.location),description:diagnosticSelectorStats(S.description),posted:diagnosticSelectorStats(S.posted)},jobposting_jsonld:diagnosticJsonLd(),headings_or_labels:diagnosticLabels(),candidate_ancestor_signatures:diagnosticDescriptionCandidates(),description_source:descriptionSource,classification:descriptionSource==='none'?(route==='standalone_detail'?'direct_route_identity_shell_or_unexpected_dom':'search_pane_description_not_found'):'substantive_description_observed'});
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
      const firstField=(selectors,attribute='')=>{for(const selector of selectors){const node=card?.querySelector?.(selector);const value=attribute?C.clean(node?.getAttribute?.(attribute)||''):C.textOf(node);if(value)return value;}return '';};
      const rawTitle=C.clean(anchor.innerText||anchor.getAttribute('aria-label')||''),observedTitle=cardTitle(anchor)||rawTitle;
      const company=firstField(['a[href*="/company/"]','[data-testid*="company"]','[data-view-name*="company"]','[class*="company"]','.job-card-container__primary-description','.artdeco-entity-lockup__subtitle','.base-search-card__subtitle']);
      const location=firstField(['[data-testid*="location"]','[data-view-name*="location"]','[class*="location"]','.job-card-container__metadata-item','.job-search-card__location','.base-search-card__metadata']);
      const posted=firstField(['time','[data-testid*="date"]','[data-view-name*="date"]','[class*="listed-time"]','[class*="posted"]'])||firstField(['time'],'datetime');
      seen.set(id,{source_job_id:id,url,title:observedTitle,title_raw:rawTitle,company,location,posted_text:posted,posted_age_days:C.parseAgeDays(posted)});
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
    const ch=C.challengeInfo();
    const wall=C.authWallInfo(S.authSignIn,/\/login|\/checkpoint|\/authwall/i),positive=C.positiveAuthInfo(S.authPositive,['my jobs','saved jobs','notifications']);
    const auth_state=ch.challenged?'challenged_cooldown':wall.required?'sign_in_required':positive.positive?'verified':'unknown';
    const authenticated=auth_state==='verified';
    return{platform:'linkedin',page_type:'auth',authenticated,auth_state,login_required:auth_state==='sign_in_required',auth_evidence:positive.reason||wall.reason||'no conclusive account evidence',challenged:ch.challenged,challenge_reason:ch.reason,page_url:location.href,reason:ch.challenged?ch.reason:(wall.required?wall.reason:(authenticated?'LinkedIn session authenticated':'LinkedIn auth state unverified'))};
  }
  function inspectSearch(){
    const ch=C.challengeInfo(),end=C.exhaustionInfo(['no matching jobs found']),url=location.href.toLowerCase();
    const wall=C.authWallInfo(S.authSignIn,/\/login|\/checkpoint|\/authwall/i);
    const results=collect();
    const verifiedEmptyReason=results.extraction_diagnostics?.empty_state_reason||'';
    const expectedRoute=/\/jobs\/search(?:\/|$)/.test(new URL(location.href).pathname.toLowerCase());
    const ready=!ch.challenged&&!wall.required&&expectedRoute&&!results.extraction_scope_missing&&(results.links.length>0||!!verifiedEmptyReason);
    const auth_state=ch.challenged?'challenged_cooldown':wall.required?'sign_in_required':ready?'verified':'unknown';
    return{platform:'linkedin',extension_build:BUILD_ID,page_type:'search',authenticated:ready,auth_state,readiness_state:ch.challenged?'challenged_cooldown':wall.required?'sign_in_required':ready?'ready':'unknown',ready,challenged:ch.challenged,challenge_reason:ch.reason,login_required:wall.required,page_url:location.href,result_links:results.links,extraction_scope_missing:results.extraction_scope_missing,extraction_diagnostics:results.extraction_diagnostics,next_url:nextUrl(),exhausted:end.exhausted||!!verifiedEmptyReason,exhaustion_reason:end.reason||verifiedEmptyReason,title:document.title};
  }
  function inspectJob(route='standalone_detail'){
    const ch=C.challengeInfo(),surface=C.pageSurface(); if(surface.surface!=='job')return{platform:'linkedin',page_type:surface.surface,challenged:surface.surface==='challenge',challenge_reason:surface.reason,page_url:location.href,surface_reason:surface.reason,job:null,detail_diagnostics:detailDiagnostics('standalone_detail',sid(location.href),'none',surface)};
    const x=C.parseJsonLdJob()||{},pageTitle=C.clean(document.title).replace(/\s*\|\s*LinkedIn\s*$/i,'').split('|')[0];
    const title=x.title||C.firstText(S.title)||pageTitle,company=x.company||C.firstText(S.company),locationText=x.location||C.firstText(S.location);
    const jsonDescription=C.clean(x.description),selectorDescription=C.firstText(S.description),headingDescription=C.headingSectionText(['about the job','job description']);
    const description=jsonDescription||selectorDescription||headingDescription,descriptionSource=jsonDescription?'json_ld':selectorDescription?'selector':'heading_structural',posted=x.posted_at||C.firstText(S.posted),id=sid(location.href),url=canon(location.href);
    return{platform:'linkedin',page_type:'job',surface:'job',challenged:ch.challenged,challenge_reason:ch.reason,page_url:location.href,extraction_source:descriptionSource,detail_acquisition:{mode:route,url:location.href},detail_diagnostics:detailDiagnostics(route,id,description?descriptionSource:'none',surface),job:{source_job_id:id,canonical_url:url,apply_url:x.apply_url||'',title:C.normalizeTitle(title),company:C.clean(company),location:C.clean(locationText),remote_status:/remote|work from home|wfh/i.test(`${locationText} ${description.slice(0,2500)}`)?'remote':'unknown',employment_type:C.clean(x.employment_type),salary_text:C.clean(x.salary_text),posted_at:C.clean(posted),posted_age_days:C.parseAgeDays(posted),valid_through:C.clean(x.valid_through),description:C.clip(description)}};
  }
  function searchPaneSelection(sourceId){
    const scope=locateSearchResults(),target=String(sourceId||''),anchor=[...scope.cards].flatMap(card=>[...card.querySelectorAll(JOB_LINK_SELECTOR)].map(node=>({card,node}))).find(item=>sid(C.absoluteUrl(rawHref(item.node)))===target);
    if(!anchor)return{selected:false,selection_attempted:false,identity_status:'MISSING_CARD',search_pane_diagnostics:detailDiagnostics('search_pane',target,'none',C.pageSurface())};
    const expectedTitle=C.normalizeTitle(C.clean(anchor.node.getAttribute('aria-label')||anchor.node.innerText||'').replace(/\s+with verification$/i,''));
    let clickAttempted=false;try{if(typeof anchor.node.click==='function'){anchor.node.click();clickAttempted=true;}}catch(_){ }
    return{selected:clickAttempted,selected_title:expectedTitle,selected_source_job_id:target,current_job_id:sid(location.href),selection_attempted:true,card_metadata_diagnostics:cardMetadataDiagnostics(anchor.card,anchor.node),search_pane_diagnostics:detailDiagnostics('search_pane',target,'none',C.pageSurface())};
  }
  async function inspectSearchPane(sourceId,select=true){
    const searchPath=/\/jobs\/search(?:\/|$)/i.test(new URL(location.href).pathname);
    let last=searchPaneSelection(sourceId);if(select&&!last.selected)return last;
    for(let i=0;i<10;i++){
      const ch=C.challengeInfo();if(ch.challenged)return{...last,page_type:'challenge',challenged:true,challenge_reason:ch.reason};
      const wall=C.authWallInfo(S.authSignIn,/\/login|\/checkpoint|\/authwall/i);if(wall.required)return{...last,page_type:'login',login_required:true,surface_reason:wall.reason};
      if(!searchPath||!/\/jobs\/search(?:\/|$)/i.test(new URL(location.href).pathname))return{...last,page_type:'error',navigation_context_lost:true,surface_reason:'search context lost after card selection',page_url:location.href};
      if(last.current_job_id===String(sourceId||'')||last.selected_title){
        const pane=inspectJob('search_pane');
        const title=C.normalizeTitle(pane.job?.title||''),identityProven=!!title&&title===last.selected_title;
        if(pane.page_type==='job'&&String(pane.job?.description||'').trim())return{...last,...pane,surface:'embedded_search_pane',search_pane:true,selected_source_job_id:String(sourceId||''),identity_status:identityProven?'PROVEN':'MISMATCH',identity_proven:identityProven,acquisition_mode:'search_pane',acquisition_url:location.href,detail_acquisition:{mode:'search_pane',surface:'embedded_search_pane',url:location.href},job:{...pane.job,source_job_id:String(sourceId||pane.job?.source_job_id||'')},search_pane_diagnostics:detailDiagnostics('search_pane',sourceId,pane.extraction_source||'none',pane.detail_diagnostics?.page_surface||C.pageSurface())};
      }
      await new Promise(r=>setTimeout(r,300));last=searchPaneSelection(sourceId,false);
    }
    const pane=last.current_job_id===String(sourceId||'')?inspectJob('search_pane'):null;
    return pane?{...last,...pane,selected_source_job_id:String(sourceId||''),identity_status:'MISMATCH',identity_proven:false,acquisition_mode:'search_pane',acquisition_url:location.href,detail_acquisition:{mode:'search_pane',url:location.href},search_pane_diagnostics:detailDiagnostics('search_pane',sourceId,pane.extraction_source||'none',pane.detail_diagnostics?.page_surface||C.pageSurface())}:last;
  }
  async function inspectSearchEventually(){
    let page=inspectSearch();
    for(let i=0;i<8;i++){
      const needsHydration=(page.result_links||[]).some(link=>link.title&&link.title===link.title_raw&&/\s+with verification$/i.test(link.title));
      if(!needsHydration)return page;
      await new Promise(r=>setTimeout(r,300));page=inspectSearch();
    }
    return page;
  }
  function inspect(){const p=location.pathname.toLowerCase();if(p.includes('/my-items/')||p.includes('/login')||p.includes('/checkpoint')||p.includes('/authwall'))return inspectAuth();if(p.includes('/jobs/view/'))return inspectJob();return inspectSearch();}
  function advance(){const button=S.nextButtons.map(s=>document.querySelector(s)).find(Boolean);if(button){button.click();return{advanced:true,method:'click'};}return{advanced:false};}
  // Give the SPA a short hydration window, but do not add several seconds to
  // every detail when the page has no usable description container. The job is
  // still persisted as failed/retryable rather than being promoted without a
  // description.
  async function inspectJobEventually(){let last=inspectJob();for(let i=0;i<6&&last.page_type==='job'&&(!last.job?.title||!last.job?.description);i++){await new Promise(r=>setTimeout(r,450));last=inspectJob();}return last;}
  chrome.runtime.onMessage.addListener((m,_s,send)=>{if(m?.type==='JOBBOT_INSPECT_AUTH'){send(inspectAuth());return true;}if(m?.type==='JOBBOT_INSPECT_SEARCH'){send(inspectSearch());return true;}if(m?.type==='JOBBOT_INSPECT_SEARCH_EVENTUALLY'){inspectSearchEventually().then(send);return true;}if(m?.type==='JOBBOT_INSPECT_SEARCH_PANE'){inspectSearchPane(m.source_job_id,m.select!==false).then(send);return true;}if(m?.type==='JOBBOT_INSPECT_DETAIL'){inspectJobEventually().then(send);return true;}if(m?.type==='JOBBOT_INSPECT'){send(inspect());return true;}if(m?.type==='JOBBOT_SCROLL_AND_INSPECT'){C.scrollResults();setTimeout(()=>inspectSearchEventually().then(send),Math.max(700,Math.min(3500,Number(m.wait_ms||1500))));return true;}if(m?.type==='JOBBOT_ADVANCE_SEARCH'){const r=advance();setTimeout(()=>send({...r,page_url:location.href}),r.advanced?1500:0);return true;}return false;});
})();
