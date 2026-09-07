(() => {
  'use strict';
  const clean = (v) => String(v ?? '').replace(/\s+/g, ' ').trim();
  const clip = (v, n=160000) => clean(v).slice(0,n);
  const textOf = (el) => clean(el?.innerText || el?.textContent || '');
  const firstText = (selectors) => {
    for (const s of selectors) { const el=document.querySelector(s); const t=textOf(el); if(t) return t; }
    return '';
  };
  const headingSectionText = (labels) => {
    const wanted=(labels||[]).map(x=>clean(x).toLowerCase()).filter(Boolean);
    for(const h of document.querySelectorAll('h1,h2,h3,[role="heading"]')){
      const heading=textOf(h).toLowerCase();
      if(!wanted.some(x=>heading===x||heading.includes(x))) continue;
      let node=h;
      for(let i=0;i<6&&node;i++){
        node=node.parentElement;
        const t=textOf(node);
        if(t.length>=180&&t.length<=60000){
          const label=textOf(h);
          return clean(t.replace(new RegExp(`^${label.replace(/[.*+?^${}()|[\\]\\\\]/g,'\\\\$&')}\\s*`,'i'),'')).slice(0,160000);
        }
      }
    }
    return '';
  };
  const firstAttr = (selectors, attr) => {
    for (const s of selectors) { const el=document.querySelector(s); const v=clean(el?.getAttribute?.(attr)); if(v) return v; }
    return '';
  };
  const absoluteUrl = (href) => { try { return new URL(href, location.href).href; } catch (_) { return ''; } };
  const challengeInfo = () => {
    const body=clean(document.body?.innerText||'').toLowerCase(); const title=clean(document.title).toLowerCase(); const url=location.href.toLowerCase();
    const signals=['additional verification required','verify you are human','security check','checking your browser','just a moment','captcha','unusual traffic','access denied','challenge-platform','cf-chl','security verification','challenge required'];
    const hit=signals.find((x)=>body.includes(x)||title.includes(x)||url.includes(x));
    return hit?{challenged:true,reason:hit}:{challenged:false,reason:''};
  };
  const parseAgeDays = (raw) => {
    const s=clean(raw).toLowerCase(); if(!s) return null;
    if(/just posted|today|new job|minutes? ago|\b\d+\s*m(?:in)?\b/.test(s)) return 0;
    let m=s.match(/(\d+)\s*(?:h|hr|hrs|hour|hours)\b/); if(m) return 0;
    m=s.match(/(\d+)\s*(?:d|day|days)\+?\b/); if(m) return Number(m[1]);
    m=s.match(/(\d+)\s*(?:w|wk|wks|week|weeks)\b/); if(m) return Number(m[1])*7;
    m=s.match(/(\d+)\s*(?:mo|month|months)\b/); if(m) return Number(m[1])*30;
    return null;
  };
  const parseJsonLdJob = () => {
    for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        const parsed=JSON.parse(s.textContent||'{}');
        const stack=Array.isArray(parsed)?parsed:[parsed];
        while(stack.length){
          const item=stack.shift(); if(!item||typeof item!=='object') continue;
          if(Array.isArray(item['@graph'])) stack.push(...item['@graph']);
          const typ=Array.isArray(item['@type'])?item['@type'].join(' '):String(item['@type']||'');
          if(!/jobposting/i.test(typ)) continue;
          const org=item.hiringOrganization;
          const locs=Array.isArray(item.jobLocation)?item.jobLocation:(item.jobLocation?[item.jobLocation]:[]);
          const locationText=locs.map((z)=>{const a=z?.address||{}; return [a.addressLocality,a.addressRegion,a.addressCountry].filter(Boolean).join(', ');}).filter(Boolean).join(' / ');
          const sal=item.baseSalary?.value||item.baseSalary||{}; let salaryText='';
          if(sal&&typeof sal==='object'){const min=sal.minValue??sal.value?.minValue; const max=sal.maxValue??sal.value?.maxValue; const unit=sal.unitText??sal.value?.unitText; if(min!=null||max!=null) salaryText=[min,max].filter(v=>v!=null).join(' - ')+(unit?` / ${unit}`:'');}
          return {title:clean(item.title),company:clean(typeof org==='string'?org:org?.name),location:clean(locationText||item.jobLocationType),employment_type:clean(Array.isArray(item.employmentType)?item.employmentType.join(', '):item.employmentType),posted_at:clean(item.datePosted),valid_through:clean(item.validThrough),description:clip(item.description),salary_text:clean(salaryText),apply_url:absoluteUrl(item.url||'')};
        }
      } catch(_){}
    }
    return null;
  };
  const scrollResults = () => {
    const containers=[...document.querySelectorAll('[role="main"], main, [class*="jobs-search-results-list"], [class*="JobsList"], [class*="jobsearch-ResultsList"]')];
    for(const el of containers){ try{ if(el.scrollHeight>el.clientHeight+100) el.scrollTo({top:el.scrollHeight,behavior:'smooth'}); }catch(_){} }
    window.scrollTo({top:document.documentElement.scrollHeight,behavior:'smooth'});
  };
  globalThis.JobBotCommon={clean,clip,textOf,firstText,headingSectionText,firstAttr,absoluteUrl,challengeInfo,parseAgeDays,parseJsonLdJob,scrollResults};
})();
