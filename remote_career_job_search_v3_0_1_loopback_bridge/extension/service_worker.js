'use strict';

let bridgeConfig=null, requestSeq=1, activeRunId=null, runPromise=null;
const AUTH_URLS={
  linkedin:'https://www.linkedin.com/jobs/',
  indeed:'https://www.indeed.com/',
  glassdoor:'https://www.glassdoor.com/Job/index.htm',
};
const sleep=(ms)=>new Promise(r=>setTimeout(r,ms));

async function loadBridge(){
  if(bridgeConfig?.port&&bridgeConfig?.token)return bridgeConfig;
  const x=await chrome.storage.local.get('jobbot_bridge_config');
  const b=x.jobbot_bridge_config||null;
  if(b&&Number(b.port)>0&&String(b.token||'')){bridgeConfig={port:Number(b.port),token:String(b.token)};return bridgeConfig;}
  throw new Error('Local JobBot bridge is not configured. Start the run from the .command launcher.');
}
async function configureBridge(port,token){
  const p=Number(port),t=String(token||'');
  if(!Number.isInteger(p)||p<1||p>65535||t.length<20)throw new Error('Invalid local bridge configuration');
  bridgeConfig={port:p,token:t};
  await chrome.storage.local.set({jobbot_bridge_config:bridgeConfig});
  const health=await nativeRequest('ping',{},10000);
  if(!health?.ok)throw new Error(health?.error||'Local bridge ping failed');
  return health;
}
function transientBridgeError(error){
  const s=String(error?.message||error||'').toLowerCase();
  return /unavailable|network|failed to fetch|connection|timed out|abort|temporar|503|502|504/.test(s);
}
async function nativeRequest(action,payload={},timeoutMs=60000){
  const b=await loadBridge();
  const request_id=`r${Date.now()}_${requestSeq++}`;
  let last=null;
  for(let attempt=0;attempt<3;attempt++){
    const ctrl=new AbortController(); const timer=setTimeout(()=>ctrl.abort(),timeoutMs);
    try{
      const r=await fetch(`http://127.0.0.1:${b.port}/rpc`,{
        method:'POST',
        headers:{'Content-Type':'application/json','X-JobBot-Token':b.token},
        body:JSON.stringify({request_id,action,...payload}),
        cache:'no-store', signal:ctrl.signal,
      });
      let obj=null; try{obj=await r.json();}catch(_){throw new Error(`Local bridge returned HTTP ${r.status} with invalid JSON`);}
      if(!r.ok)throw new Error(obj?.message||obj?.error||`Local bridge HTTP ${r.status}`);
      return obj;
    }catch(e){
      last=e?.name==='AbortError'?new Error(`local bridge request timed out: ${action}`):new Error(`Local bridge unavailable: ${e?.message||e}`);
      if(!transientBridgeError(last)||attempt===2)throw last;
      await sleep(350*(attempt+1));
    }finally{clearTimeout(timer);}
  }
  throw last||new Error(`local bridge request failed: ${action}`);
}
async function waitTabComplete(tabId,timeoutMs=60000){const deadline=Date.now()+timeoutMs;while(Date.now()<deadline){const tab=await chrome.tabs.get(tabId);if(tab.status==='complete')return tab;await sleep(400);}throw new Error('page load timed out');}
async function inspectTab(tabId,type='JOBBOT_INSPECT',extra={},retries=10){for(let i=0;i<retries;i++){try{await waitTabComplete(tabId,60000);const resp=await chrome.tabs.sendMessage(tabId,{type,...extra});if(resp)return resp;}catch(e){if(i===retries-1)throw e;}await sleep(700+i*220);}throw new Error('content script did not respond');}
function fp(items){return (items||[]).map(x=>x.source_job_id||x.url).filter(Boolean).sort().join('|');}
function parseCheckpoint(raw){try{return typeof raw==='string'?JSON.parse(raw||'{}'):(raw||{});}catch(_){return {};}}

async function checkAuth(platform,runId){
  const url=AUTH_URLS[platform]; if(!url)return {authenticated:true,page:{reason:'no auth check configured'}};
  const tab=await chrome.tabs.create({url,active:true});
  try{
    const p=await inspectTab(tab.id,'JOBBOT_INSPECT_AUTH',{},5);
    const authenticated=!!p.authenticated&&!p.challenged;
    await nativeRequest('platform_auth_result',{run_id:runId,platform,authenticated,reason:p.reason||p.challenge_reason||'',page_url:p.page_url||''});
    return {authenticated,page:p};
  }finally{try{await chrome.tabs.remove(tab.id);}catch(_){}}
}

async function gatherStableSearch(tabId,initial){
  let page=initial; const merged=new Map((page.result_links||[]).map(x=>[x.source_job_id||x.url,x])); let stable=0;
  for(let i=0;i<6&&stable<2;i++){
    const before=merged.size;
    const after=await inspectTab(tabId,'JOBBOT_SCROLL_AND_INSPECT',{wait_ms:1200+i*150});
    if(after.challenged)return after;
    for(const x of (after.result_links||[]))merged.set(x.source_job_id||x.url,x);
    page.next_url=page.next_url||after.next_url||'';
    page.page_url=after.page_url||page.page_url;
    stable=merged.size===before?stable+1:0;
  }
  page.result_links=[...merged.values()]; return page;
}

async function advanceSearch(tabId,page){
  if(page.next_url){await chrome.tabs.update(tabId,{url:page.next_url,active:true});await sleep(900);return {advanced:true,url:page.next_url};}
  try{
    const r=await inspectTab(tabId,'JOBBOT_ADVANCE_SEARCH',{},3);
    if(r?.advanced){await sleep(1200);return {advanced:true,url:r.page_url||''};}
  }catch(_){}
  return {advanced:false,url:''};
}

async function processTask(runId,task){
  const taskId=Number(task.task_id), platform=String(task.platform||'');
  const maxResults=task.max_results==null?null:Number(task.max_results), windowDays=Number(task.window_days||30);
  const cp=parseCheckpoint(task.checkpoint_json); let searchUrl=cp.search_url||task.search_url;
  let processed=Number(task.jobs_recorded||0), resultsSeen=Number(task.results_seen||0), pagesVisited=Number(task.pages_visited||0), detailRead=Number(task.detail_count_read||0);
  const seenPageFingerprints=new Set(); let searchTab=null,detailTab=null;
  try{
    await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'navigation',message:`open search ${searchUrl}`});
    searchTab=await chrome.tabs.create({url:searchUrl,active:true});
    while(true){
      const stop=await nativeRequest('should_stop',{run_id:runId}); if(stop.stop){await nativeRequest('complete_task',{run_id:runId,task_id:taskId,status:'stopped',reason:'stop requested'});return;}
      let page=await inspectTab(searchTab.id,'JOBBOT_INSPECT_SEARCH');
      if(page.challenged){await nativeRequest('pause_platform',{run_id:runId,platform,reason:page.challenge_reason||'platform challenge'});return;}
      if(page.login_required){await nativeRequest('platform_auth_result',{run_id:runId,platform,authenticated:false,reason:`${platform} session is no longer authenticated`,page_url:page.page_url||''});return;}
      page=await gatherStableSearch(searchTab.id,page);
      if(page.challenged){await nativeRequest('pause_platform',{run_id:runId,platform,reason:page.challenge_reason||'platform challenge'});return;}
      const items=page.result_links||[], pageFp=fp(items);
      if(pageFp&&seenPageFingerprints.has(pageFp)){await nativeRequest('complete_task',{run_id:runId,task_id:taskId,status:'completed',reason:'repeated result-page fingerprint; exhausted',exhausted:true});return;}
      if(pageFp)seenPageFingerprints.add(pageFp);
      pagesVisited+=1; resultsSeen+=items.length;
      await nativeRequest('task_progress',{run_id:runId,task_id:taskId,results_seen:resultsSeen,pages_visited:pagesVisited,checkpoint:{search_url:page.page_url||searchUrl,page_fingerprint:pageFp,processed,page_number:pagesVisited,scroll_generation:pagesVisited}});

      let eligibleCards=0, recordedThisPage=0;
      for(let idx=0;idx<items.length;idx++){
        const link=items[idx]; const age=link.posted_age_days;
        if(age!=null&&age>windowDays)continue;
        eligibleCards+=1;
        await nativeRequest('record_result',{run_id:runId,task_id:taskId,source_site:platform,source_job_id:link.source_job_id||'',source_url:link.url||''});
        if(maxResults!==null&&processed>=maxResults){await nativeRequest('complete_task',{run_id:runId,task_id:taskId,status:'test_limit',reason:`Acceptance limit reached (${maxResults}); production has no count limit`});return;}
        await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'navigation',message:`open detail ${link.source_job_id||link.url}`});
        if(!detailTab)detailTab=await chrome.tabs.create({url:'about:blank',active:false});
        await chrome.tabs.update(detailTab.id,{url:link.url,active:true});
        let detail;
        try{detail=await inspectTab(detailTab.id,'JOBBOT_INSPECT_DETAIL');}catch(e){await nativeRequest('job_error',{run_id:runId,task_id:taskId,message:String(e?.message||e),url:link.url}).catch(()=>{});await chrome.tabs.update(searchTab.id,{active:true}).catch(()=>{});continue;}
        if(detail.challenged){await nativeRequest('pause_platform',{run_id:runId,platform,reason:detail.challenge_reason||'challenge on job detail'});return;}
        detailRead+=1; await nativeRequest('detail_read',{run_id:runId,task_id:taskId,source_site:platform,source_job_id:link.source_job_id||detail.job?.source_job_id||'',source_url:link.url||detail.job?.canonical_url||''});
        if(detail.job?.title&&detail.job?.canonical_url){
          const rec=await nativeRequest('record_job',{run_id:runId,task_id:taskId,job:{...detail.job,search_card:link,page_url:detail.page_url||link.url}},90000);
          if(rec.ok){processed+=1;recordedThisPage+=1;}
          else await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'job_rejected',message:`record_job rejected: ${rec.error||rec.message||'unknown reason'} (${detail.job.title})`}).catch(()=>{});
        } else await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'extraction_miss',message:`detail payload incomplete type=${detail?.page_type||'none'} title=${detail?.job?.title||'none'} canonical=${detail?.job?.canonical_url||'none'} page=${detail?.page_url||'none'} source=${link.source_job_id||link.url}`}).catch(()=>{});
        await chrome.tabs.update(searchTab.id,{active:true}).catch(()=>{});
        await nativeRequest('task_progress',{run_id:runId,task_id:taskId,results_seen:resultsSeen,pages_visited:pagesVisited,checkpoint:{search_url:page.page_url||searchUrl,page_fingerprint:pageFp,last_job_key:link.source_job_id||link.url,last_job_index:idx,processed}});
        const stopAfter=await nativeRequest('should_stop',{run_id:runId});
        if(stopAfter.stop||stopAfter.stop_after_current){await nativeRequest('complete_task',{run_id:runId,task_id:taskId,status:'stopped',reason:stopAfter.stop?'emergency stop requested':'stop after current job requested'});return;}
        await sleep(350);
      }
      if(maxResults!==null&&processed>=maxResults){await nativeRequest('complete_task',{run_id:runId,task_id:taskId,status:'test_limit',reason:`Acceptance limit reached (${maxResults}); production has no count limit`});return;}

      await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'navigation',message:'advance search result batch/page'});
      const adv=await advanceSearch(searchTab.id,page);
      if(!adv.advanced){await nativeRequest('complete_task',{run_id:runId,task_id:taskId,status:'exhausted',reason:items.length===0?'no results / exhausted':'no next page or additional result batch',exhausted:true});return;}
      searchUrl=adv.url||page.next_url||searchUrl;
      await sleep(700);
    }
  }catch(e){await nativeRequest('complete_task',{run_id:runId,task_id:taskId,status:'failed',reason:String(e?.message||e).slice(0,700)}).catch(()=>{});}
  finally{if(detailTab?.id)try{await chrome.tabs.remove(detailTab.id);}catch(_){} if(searchTab?.id)try{await chrome.tabs.remove(searchTab.id);}catch(_){} }
}

async function runProduction(runId){
  activeRunId=Number(runId); await chrome.storage.local.set({jobbot_active_run_id:activeRunId});
  try{
    const begin=await nativeRequest('begin_run',{run_id:activeRunId}); if(!begin.ok)throw new Error(begin.error||'cannot begin run');
    const authChecked=new Map();
    while(true){
      const n=await nativeRequest('next_task',{run_id:activeRunId,worker_id:`extension-run-${activeRunId}`}); if(n.stop||n.done)break; if(!n.task)break;
      const p=String(n.task.platform||'');
      if(!authChecked.has(p)){
        try{const a=await checkAuth(p,activeRunId); authChecked.set(p,a.authenticated);}catch(e){authChecked.set(p,false);await nativeRequest('platform_auth_result',{run_id:activeRunId,platform:p,authenticated:false,reason:`auth probe failed: ${e?.message||e}`}).catch(()=>{});}
        if(!authChecked.get(p))continue;
      }
      if(!authChecked.get(p))continue;
      await processTask(activeRunId,n.task);
    }
    const fin=await nativeRequest('finish_run',{run_id:activeRunId});
    await chrome.storage.local.remove('jobbot_active_run_id'); activeRunId=null; return {ok:true,status:fin.status};
  }catch(e){
    await nativeRequest('run_error',{run_id:activeRunId,message:String(e?.message||e).slice(0,700)}).catch(()=>{});
    // Keep active_run_id so the alarm can resume after a temporary bridge or Chrome failure.
    throw e;
  }
}

async function ensureResume(){
  if(runPromise)return;
  try{
    const x=await chrome.storage.local.get('jobbot_active_run_id'); const rid=Number(x.jobbot_active_run_id||0); if(!rid)return;
    const st=await nativeRequest('run_status',{run_id:rid}); const status=st?.run?.status;
    if(st?.ok&&!['completed','partial','stopped','failed'].includes(status)){
      runPromise=runProduction(rid).catch(()=>{}).finally(()=>{runPromise=null;});
    }
  }catch(_){}
}

chrome.runtime.onMessage.addListener((msg,_sender,sendResponse)=>{
  if(msg?.type==='JOBBOT_CONFIGURE_BRIDGE'){configureBridge(msg.port,msg.token).then(x=>sendResponse({ok:true,version:x.version,bridge:'loopback'})).catch(e=>sendResponse({ok:false,error:String(e?.message||e)}));return true;}
  if(msg?.type==='JOBBOT_START_RUN'){
    const rid=Number(msg.run_id||0); if(!rid){sendResponse({ok:false,error:'missing run_id'});return false;}
    if(!runPromise){runPromise=runProduction(rid).catch(()=>{}).finally(()=>{runPromise=null;});}
    sendResponse({ok:true,started:true,run_id:rid}); return false;
  }
  if(msg?.type==='JOBBOT_STOP_AFTER_CURRENT'||msg?.type==='JOBBOT_EMERGENCY_STOP'){
    const rid=Number(msg.run_id||activeRunId||0); if(!rid){sendResponse({ok:false,error:'no_run_id'});return false;}
    nativeRequest(msg.type==='JOBBOT_EMERGENCY_STOP'?'emergency_stop':'request_stop',{run_id:rid}).then(sendResponse).catch(e=>sendResponse({ok:false,error:String(e?.message||e)}));return true;
  }
  if(msg?.type==='JOBBOT_GET_STATUS'){
    const rid=Number(msg.run_id||activeRunId||0); if(!rid){sendResponse({ok:false,error:'no_run_id'});return false;}
    nativeRequest('run_status',{run_id:rid}).then(sendResponse).catch(e=>sendResponse({ok:false,error:String(e?.message||e)}));return true;
  }
  if(msg?.type==='JOBBOT_PING'){nativeRequest('ping').then(sendResponse).catch(e=>sendResponse({ok:false,error:String(e?.message||e)}));return true;}
  return false;
});
chrome.runtime.onStartup.addListener(()=>{ensureResume();});
chrome.runtime.onInstalled.addListener(()=>{chrome.alarms.create('jobbot-resume',{periodInMinutes:1});});
chrome.alarms.onAlarm.addListener((a)=>{if(a.name==='jobbot-resume')ensureResume();});
ensureResume();
