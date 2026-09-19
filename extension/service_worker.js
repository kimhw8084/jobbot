'use strict';

let bridgeConfig=null, requestSeq=1, activeRunId=null, activeTaskId=null, runPromise=null, heartbeatTimer=null;
const JOBBOT_EXTENSION_BUILD=String(chrome.runtime?.getManifest?.().version_name||'unknown');
let runtimeConfig={heartbeat_seconds:20,lease_seconds:180,watchdog_stall_seconds:180};
const MAX_IDENTICAL_FINGERPRINTS=3;
const LINKEDIN_SCOPE_RECOVERY_MAX_ATTEMPTS=2,LINKEDIN_SCOPE_REINSPECT_WAIT_MS=350,LINKEDIN_SCOPE_RELOAD_WAIT_MS=700;
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
  runtimeConfig=await requiredRequest('runtime_config',{},10000);
  return health;
}
async function createBackgroundTarget(url){
  // Keep crawler pages in an owned minimized window. active:false alone can
  // still attach a new tab to the user's foreground window/Space.
  if(typeof chrome.windows?.create==='function'){
    const win=await chrome.windows.create({url,focused:false,state:'minimized',type:'normal'});
    const tab=win?.tabs?.[0];
    if(win?.id!=null&&tab?.id!=null)return{tab,window_id:win.id,owned_window:true};
    if(win?.id!=null)try{await chrome.windows.remove(win.id);}catch(_){}
  }
  const tab=await chrome.tabs.create({url,active:false});
  return{tab,window_id:tab?.windowId??null,owned_window:false};
}
async function keepBackgroundTab(tabId,windowId){
  const tab=await chrome.tabs.get(tabId);
  if(windowId!=null&&tab.windowId!==windowId)await chrome.tabs.move(tabId,{windowId,index:-1});
  await chrome.tabs.update(tabId,{active:false});
  return chrome.tabs.get(tabId);
}
async function closeBackgroundTarget(target,tabIds=[]){
  if(target?.owned_window&&target.window_id!=null)try{await chrome.windows.remove(target.window_id);}catch(_){}
  for(const id of [...new Set([target?.tab?.id,...tabIds].filter(x=>x!=null))])try{await chrome.tabs.remove(id);}catch(_){}
}
function transientBridgeError(error){
  const s=String(error?.message||error||'').toLowerCase();
  return /unavailable|network|failed to fetch|connection|could not establish connection|receiving end does not exist|timed out|abort|temporar|503|502|504/.test(s);
}
function authProbeReceiverFailure(error){
  const s=String(error?.message||error||'').toLowerCase();
  return /content script did not respond|could not establish connection|receiving end does not exist|message port closed before a response was received/.test(s);
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
      await chrome.storage.local.set({jobbot_bridge_state:{status:'connected',port:b.port,action,at:new Date().toISOString(),error:''}});
      return obj;
    }catch(e){
      last=e?.name==='AbortError'?new Error(`Local bridge timed out at 127.0.0.1:${b.port} during ${action}`):new Error(`Local bridge unavailable at 127.0.0.1:${b.port} during ${action}: ${e?.message||e}`);
      if(!transientBridgeError(last)||attempt===2){await chrome.storage.local.set({jobbot_bridge_state:{status:'error',port:b.port,action,at:new Date().toISOString(),error:last.message}}).catch(()=>{});throw last;}
      await sleep(350*(attempt+1));
    }finally{clearTimeout(timer);}
  }
  throw last||new Error(`local bridge request failed: ${action}`);
}
function requireRpcOk(response,action,context={}){
  if(!response||response.ok!==true){
    const error=new Error(`RPC ${action} failed: ${response?.error||response?.message||'bridge returned ok=false'}`);
    error.rpcAction=action; error.rpcContext=context; throw error;
  }
  return response;
}
async function deploymentIdentity(){
  try{
    const response=await fetch(chrome.runtime.getURL('deployment_identity.json'),{cache:'no-store'});
    if(!response.ok)return{state:'missing',error:`deployment identity returned HTTP ${response.status}`};
    const value=await response.json();
    return value&&typeof value==='object'?{state:'present',...value}:{state:'invalid'};
  }catch(e){return{state:'unavailable',error:String(e?.message||e)}}
}
async function requiredRequest(action,payload={},timeoutMs=60000){
  return requireRpcOk(await nativeRequest(action,payload,timeoutMs),action,payload);
}
async function reportExtensionBuild(runId=0,expectedBuild='',refreshId=''){
  const deployment_identity=await deploymentIdentity();
  return nativeRequest('extension_build',{
    run_id:Number(runId||0), build:JOBBOT_EXTENSION_BUILD,
    expected_build:String(expectedBuild||JOBBOT_EXTENSION_BUILD), refresh_id:String(refreshId||''), deployment_identity,
  },10000);
}
async function requestExtensionRefresh(runId=0,expectedBuild='',refreshId=''){
  const expected=String(expectedBuild||JOBBOT_EXTENSION_BUILD), rid=Number(runId||0), key=String(refreshId||'');
  if(!expected)throw new Error('missing expected extension build');
  if(JOBBOT_EXTENSION_BUILD===expected){
    const signal=await reportExtensionBuild(rid,expected,key);
    if(!signal?.ok){
      await nativeRequest('extension_refresh_failed',{
        refresh_id:key,
        error:String(signal?.error||'bootstrap_or_deployment_source_mismatch'),
      },10000).catch(()=>{});
      return signal;
    }
    return {...signal,refresh_id:signal.refresh_id||key,status:signal.status||'confirmed',refreshed:false,reload_required:false};
  }
  const request=await nativeRequest('extension_refresh',{run_id:rid,expected_build:expected,refresh_id:key},10000);
  if(!request?.ok)return request;
  const reloading=await nativeRequest('extension_refresh_reloading',{refresh_id:request.refresh_id||key},10000);
  if(!reloading?.ok)return reloading;
  await chrome.storage.local.set({jobbot_expected_extension_build:expected,jobbot_refresh_id:request.refresh_id||key});
  return {...reloading,loaded_build:JOBBOT_EXTENSION_BUILD,reload_required:true,refreshed:false};
}
async function confirmStoredBuild(){
  const x=await chrome.storage.local.get(['jobbot_expected_extension_build','jobbot_refresh_id','jobbot_active_run_id']);
  const expected=String(x.jobbot_expected_extension_build||''), refreshId=String(x.jobbot_refresh_id||'');
  if(!expected||!refreshId||expected!==JOBBOT_EXTENSION_BUILD)return;
  await reportExtensionBuild(Number(x.jobbot_active_run_id||0),expected,refreshId);
}
async function waitTabComplete(tabId,timeoutMs=45000){const deadline=Date.now()+timeoutMs;while(Date.now()<deadline){const tab=await chrome.tabs.get(tabId);if(tab.status==='complete')return tab;await sleep(400);}throw new Error('page load timed out');}
async function inspectTab(tabId,type='JOBBOT_INSPECT',extra={},retries=4){for(let i=0;i<retries;i++){try{await waitTabComplete(tabId,45000);const resp=await chrome.tabs.sendMessage(tabId,{type,...extra});if(resp)return resp;}catch(e){if(i===retries-1)throw e;}await sleep(700+i*220);}throw new Error('content script did not respond');}
function fp(items){return (items||[]).map(x=>x.source_job_id||x.url).filter(Boolean).sort().join('|');}
function parseCheckpoint(raw){try{return typeof raw==='string'?JSON.parse(raw||'{}'):(raw||{});}catch(_){return {};}}
function normalizeSearchUrl(raw){try{const u=new URL(raw);if(/(^|\.)linkedin\.com$/i.test(u.hostname))u.searchParams.delete('currentJobId');return u.href;}catch(_){return raw||'';}}
function searchContextStatus(requested,observed,platform){
  try{
    const r=new URL(requested),o=new URL(observed); if(r.origin!==o.origin||r.pathname!==o.pathname)return 'redirected';
    const keys=platform==='linkedin'?['keywords','location','f_TPR','f_WT','start']:platform==='indeed'?['q','l','fromage','sort','start']:['q','location','fromage','page'];
    for(const key of keys)if(r.searchParams.has(key)&&r.searchParams.get(key)!==o.searchParams.get(key))return 'query_context_lost';
    return 'verified';
  }catch(_){return 'unverified';}
}
function startHeartbeat(){
  if(heartbeatTimer)clearInterval(heartbeatTimer);
  heartbeatTimer=setInterval(()=>{if(activeRunId)nativeRequest('heartbeat',{run_id:activeRunId,task_id:activeTaskId||0},10000).catch(()=>{});},Math.max(1,Number(runtimeConfig.heartbeat_seconds||20))*1000);
}
function stopHeartbeat(){if(heartbeatTimer)clearInterval(heartbeatTimer);heartbeatTimer=null;}

async function checkAuth(platform,runId,taskId,searchUrl=''){
  const url=AUTH_URLS[platform]; if(!url)return {authenticated:true,page:{reason:'no auth check configured'}};
  const target=await createBackgroundTarget(url),tab=target.tab;
  try{
    let p;
    try{p=await inspectTab(tab.id,'JOBBOT_INSPECT_AUTH',{},5);}
    catch(error){
      if(!authProbeReceiverFailure(error))throw error;
      p={platform,authenticated:false,auth_state:'unknown',reason:'landing auth probe receiver unavailable',page_url:url};
    }
    const authState=String(p.auth_state|| (p.authenticated?'verified':p.login_required?'sign_in_required':'unknown'));
    if(p.challenged){
      await requiredRequest('pause_platform',{run_id:runId,task_id:taskId,platform,auth_state:authState==='verified'?'verified':'unknown',reason:p.challenge_reason||p.reason||'platform challenge',page_url:p.page_url||''});
      return {authenticated:false,ready:false,auth_state:'challenged_cooldown',page:p};
    }
    if(authState==='sign_in_required'){
      await requiredRequest('platform_auth_result',{run_id:runId,task_id:taskId,platform,authenticated:false,auth_state:'sign_in_required',reason:p.reason||'explicit sign-in wall',page_url:p.page_url||'',requested_url:searchUrl||url,observed_url:p.page_url||''});
      return {authenticated:false,ready:false,auth_state:authState, page:p};
    }
    // Landing-page account heuristics are only advisory. The actual requested
    // search surface is authoritative when it is usable, and must be probed
    // even when landing-page auth evidence is unknown.
    const searchTarget=await createBackgroundTarget(searchUrl||url);
    try{
      const surface=await inspectTab(searchTarget.tab.id,'JOBBOT_INSPECT_SEARCH_EVENTUALLY',{},4);
      const observed=surface.page_url||searchUrl||url;
      if(surface.challenged){
        await requiredRequest('pause_platform',{run_id:runId,task_id:taskId,platform,auth_state:p.authenticated?'verified':'unknown',reason:`${platform} search surface challenged: ${surface.challenge_reason||'challenge'}`,requested_url:searchUrl||url,observed_url:observed});
        return {authenticated:false,ready:false,auth_state:'challenged_cooldown',page:p,search_surface:surface};
      }
      if(surface.login_required){
        await requiredRequest('platform_auth_result',{run_id:runId,task_id:taskId,platform,authenticated:false,auth_state:'sign_in_required',reason:`${platform} search surface requires sign-in`,page_url:observed,requested_url:searchUrl||url,observed_url:observed});
        return {authenticated:false,ready:false,auth_state:'sign_in_required',page:p,search_surface:surface};
      }
      if(surface.ready){
        const reason=`${platform} requested search surface ready; requested=${searchUrl||url} observed=${observed}`;
        await requiredRequest('platform_readiness',{run_id:runId,task_id:taskId,platform,status:'verified',auth_state:'verified',reason,page_url:observed,search_url:searchUrl||url});
        return {authenticated:true,ready:true,auth_state:'verified',page:p,search_surface:surface};
      }
      const reason=`${platform} requested search surface unverified; requested=${searchUrl||url} observed=${observed}`;
      await requiredRequest('platform_readiness',{run_id:runId,task_id:taskId,platform,status:'retryable',auth_state:p.authenticated?'verified':'unknown',reason,page_url:observed,search_url:searchUrl||url});
      return {authenticated:!!p.authenticated,ready:false,auth_state:p.authenticated?'verified':'unknown',page:p,search_surface:surface};
    } finally { await closeBackgroundTarget(searchTarget); }
  }finally{await closeBackgroundTarget(target);}
}

async function gatherStableSearch(tabId,initial){
  let page=initial; const merged=new Map((page.result_links||[]).map(x=>[x.source_job_id||x.url,x])); let stable=0;
  for(let i=0;i<4&&stable<1;i++){
    const before=merged.size;
    const after=await inspectTab(tabId,'JOBBOT_SCROLL_AND_INSPECT',{wait_ms:800+i*150});
    if(after.challenged||after.extraction_scope_missing)return after;
    for(const x of (after.result_links||[]))merged.set(x.source_job_id||x.url,x);
    page.next_url=page.next_url||after.next_url||'';
    page.page_url=after.page_url||page.page_url;
    stable=merged.size===before?stable+1:0;
  }
  page.result_links=[...merged.values()]; return page;
}

async function recoverLinkedInSearchScope(tabId,requestedUrl,initial,runId,taskId){
  let page=initial;
  for(let attempt=1;attempt<=LINKEDIN_SCOPE_RECOVERY_MAX_ATTEMPTS;attempt++){
    const mode=attempt===1?'same_url_reinspect':'same_url_reload';
    if(attempt===1)await sleep(LINKEDIN_SCOPE_REINSPECT_WAIT_MS);
    else{await chrome.tabs.update(tabId,{url:requestedUrl});await sleep(LINKEDIN_SCOPE_RELOAD_WAIT_MS);}
    page=await inspectTab(tabId,'JOBBOT_INSPECT_SEARCH_EVENTUALLY');
    const contextStatus=searchContextStatus(requestedUrl,page.page_url||'', 'linkedin');
    const outcome=page.challenged?'challenge':page.login_required?'login':contextStatus!=='verified'?contextStatus:page.page_type==='error'?'error':page.extraction_scope_missing?'scope_missing':'scope_restored';
    await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'search_scope_recovery',message:`LinkedIn search scope recovery attempt ${attempt}: ${outcome}`,payload:{attempt,mode,outcome,requested_search_url:requestedUrl,observed_page_url:page.page_url||'',context_status:contextStatus,extraction_scope_missing:!!page.extraction_scope_missing,extraction_diagnostics:page.extraction_diagnostics||null}}).catch(()=>{});
    if(outcome!=='scope_missing')return page;
  }
  return page;
}

async function advanceSearch(tabId,page){
  if(page.next_url){const next=normalizeSearchUrl(page.next_url);await chrome.tabs.update(tabId,{url:next});await sleep(600);return {advanced:true,url:next};}
  try{
    const r=await inspectTab(tabId,'JOBBOT_ADVANCE_SEARCH',{},3);
    if(r?.advanced){await sleep(800);return {advanced:true,url:normalizeSearchUrl(r.page_url||'')};}
  }catch(_){}
  return {advanced:false,url:''};
}

async function processTask(runId,task){
  const taskId=Number(task.task_id), platform=String(task.platform||'');
  activeTaskId=taskId;
  const maxResults=task.max_results==null?null:Number(task.max_results), windowDays=Number(task.window_days||30);
  const cp=parseCheckpoint(task.checkpoint_json); const requestedSearchUrl=normalizeSearchUrl(task.requested_search_url||task.search_url); const checkpointSearchUrl=normalizeSearchUrl(cp.search_url||''); let searchUrl=(cp.context_status==='query_context_lost'||cp.context_status==='redirected')?requestedSearchUrl:(checkpointSearchUrl||requestedSearchUrl);
  let processed=Number(task.jobs_recorded||0), resultsSeen=Number(task.results_seen||0), pagesVisited=Number(task.pages_visited||0), detailRead=Number(task.detail_count_read||0);
  let cardsExtracted=Number(task.cards_extracted||0), persistenceAttempted=Number(task.cards_persistence_attempted||0), persistenceSucceeded=Number(task.cards_persistence_succeeded||0), persistenceFailed=Number(task.cards_persistence_failed||0), duplicateCards=Number(task.duplicate_cards||0), pendingDetails=Number(task.pending_details||0), detailsFailed=Number(task.details_failed||0);
  const fingerprintCounts=new Map(); let searchTarget=null,searchTab=null,detailTab=null,lastMeaningfulAt=Date.now(),contextRecoveryAttempts=Number(task.context_recovery_attempts||cp.context_recovery_attempts||0);
  const cardStats=()=>({extracted_cards:cardsExtracted,persistence_attempted:persistenceAttempted,persistence_succeeded:persistenceSucceeded,persistence_failed:persistenceFailed,duplicate_cards:duplicateCards,pending_details:pendingDetails,details_completed:detailRead,details_failed:detailsFailed});
  const progressPayload=(page,pageFp,contextStatus='verified')=>({run_id:runId,task_id:taskId,results_seen:resultsSeen,pages_visited:pagesVisited,checkpoint:{search_url:normalizeSearchUrl(page.page_url||searchUrl),requested_search_url:requestedSearchUrl,observed_page_url:normalizeSearchUrl(page.page_url||searchUrl),context_status:contextStatus,page_fingerprint:pageFp,processed,page_number:pagesVisited,scroll_generation:pagesVisited,context_recovery_attempts:contextRecoveryAttempts,card_stats:cardStats()}});
  const finishIncomplete=async(reason)=>{try{await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'incomplete',reason});}catch(_){/* preserve the original failure when the bridge is unavailable */}};
  try{
    await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'navigation',message:`open search ${searchUrl}`});
    searchTarget=await createBackgroundTarget(searchUrl); searchTab=searchTarget.tab;
    while(true){
      const watchdogMs=Math.max(1,Number(runtimeConfig.watchdog_stall_seconds||180))*1000;
      if(Date.now()-lastMeaningfulAt>watchdogMs){await finishIncomplete(`SAFETY_STOP: watchdog observed no meaningful progress for ${runtimeConfig.watchdog_stall_seconds||180} seconds`);return;}
      const stop=await requiredRequest('should_stop',{run_id:runId}); if(stop.stop){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'stopped',reason:'stop requested'});return;}
      await keepBackgroundTab(searchTab.id,searchTarget.window_id);
      let scopeRecoveryAttempted=false;
      let page=await inspectTab(searchTab.id,'JOBBOT_INSPECT_SEARCH_EVENTUALLY');
      if(page.challenged){await requiredRequest('pause_platform',{run_id:runId,task_id:taskId,platform,reason:page.challenge_reason||'platform challenge',requested_url:searchUrl,observed_url:page.page_url||''});return;}
      if(page.login_required){await requiredRequest('platform_auth_result',{run_id:runId,task_id:taskId,platform,authenticated:false,auth_state:'sign_in_required',reason:`${platform} search surface requires sign-in`,page_url:page.page_url||'',requested_url:searchUrl,observed_url:page.page_url||''});return;}
      let contextStatus=searchContextStatus(searchUrl,page.page_url||'',platform);
      if(contextStatus==='verified'&&platform==='linkedin'&&page.extraction_scope_missing){scopeRecoveryAttempted=true;page=await recoverLinkedInSearchScope(searchTab.id,searchUrl,page,runId,taskId);contextStatus=searchContextStatus(searchUrl,page.page_url||'',platform);}
      if(page.challenged){await requiredRequest('pause_platform',{run_id:runId,task_id:taskId,platform,reason:page.challenge_reason||'platform challenge',requested_url:searchUrl,observed_url:page.page_url||''});return;}
      if(page.login_required){await requiredRequest('platform_auth_result',{run_id:runId,task_id:taskId,platform,authenticated:false,auth_state:'sign_in_required',reason:`${platform} search surface requires sign-in`,page_url:page.page_url||'',requested_url:searchUrl,observed_url:page.page_url||''});return;}
      if(contextStatus!=='verified'){
        if(contextRecoveryAttempts<2){
          contextRecoveryAttempts+=1;
          await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'search_context_recovery',message:`${contextStatus}: requested=${searchUrl} observed=${page.page_url||''}`,payload:{requested_search_url:searchUrl,observed_page_url:page.page_url||'',context_status:contextStatus,attempt:contextRecoveryAttempts}}).catch(()=>{});
          await requiredRequest('task_progress',progressPayload(page,'',contextStatus));
          await chrome.tabs.update(searchTab.id,{url:searchUrl}); await sleep(700+contextRecoveryAttempts*300); continue;
        }
        const message=`INCOMPLETE: ${contextStatus} requested_search_url=${searchUrl} observed_page_url=${page.page_url||''}`;
        await requiredRequest('task_progress',progressPayload(page,'',contextStatus));
        await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'query_context_lost',message,payload:{requested_search_url:searchUrl,observed_page_url:page.page_url||''}}).catch(()=>{});
        await finishIncomplete(message); return;
      }
      if(scopeRecoveryAttempted&&page.page_type==='error'){await finishIncomplete(`SAFETY_STOP: ${platform} search error surface at ${page.page_url||searchUrl}`);return;}
      if(page.extraction_scope_missing){const message=`${platform} search extraction scope missing at ${page.page_url}; diagnostics=${JSON.stringify(page.extraction_diagnostics||{})}`;await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'extraction_scope_missing',message});await finishIncomplete(`SAFETY_STOP: ${message}`);return;}
      page=await gatherStableSearch(searchTab.id,page);
      if(page.challenged){await requiredRequest('pause_platform',{run_id:runId,task_id:taskId,platform,reason:page.challenge_reason||'platform challenge',requested_url:searchUrl,observed_url:page.page_url||''});return;}
      if(page.login_required){await requiredRequest('platform_auth_result',{run_id:runId,task_id:taskId,platform,authenticated:false,auth_state:'sign_in_required',reason:`${platform} search surface requires sign-in`,page_url:page.page_url||'',requested_url:searchUrl,observed_url:page.page_url||''});return;}
      contextStatus=searchContextStatus(searchUrl,page.page_url||'',platform);
      if(contextStatus!=='verified'){const message=`INCOMPLETE: ${contextStatus} after bounded recovery requested_search_url=${searchUrl} observed=${page.page_url||''}`;await requiredRequest('task_progress',progressPayload(page,'',contextStatus));await finishIncomplete(message);return;}
      if(platform==='linkedin'&&page.extraction_scope_missing){scopeRecoveryAttempted=true;page=await recoverLinkedInSearchScope(searchTab.id,searchUrl,page,runId,taskId);}
      if(page.challenged){await requiredRequest('pause_platform',{run_id:runId,task_id:taskId,platform,reason:page.challenge_reason||'platform challenge',requested_url:searchUrl,observed_url:page.page_url||''});return;}
      if(page.login_required){await requiredRequest('platform_auth_result',{run_id:runId,task_id:taskId,platform,authenticated:false,auth_state:'sign_in_required',reason:`${platform} search surface requires sign-in`,page_url:page.page_url||'',requested_url:searchUrl,observed_url:page.page_url||''});return;}
      contextStatus=searchContextStatus(searchUrl,page.page_url||'',platform);
      if(contextStatus!=='verified'&&!scopeRecoveryAttempted){const message=`INCOMPLETE: ${contextStatus} after bounded recovery requested_search_url=${searchUrl} observed=${page.page_url||''}`;await requiredRequest('task_progress',progressPayload(page,'',contextStatus));await finishIncomplete(message);return;}
      if(contextStatus!=='verified'){
        if(contextRecoveryAttempts<2){
          contextRecoveryAttempts+=1;
          await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'search_context_recovery',message:`${contextStatus}: requested=${searchUrl} observed=${page.page_url||''}`,payload:{requested_search_url:searchUrl,observed_page_url:page.page_url||'',context_status:contextStatus,attempt:contextRecoveryAttempts}}).catch(()=>{});
          await requiredRequest('task_progress',progressPayload(page,'',contextStatus));
          await chrome.tabs.update(searchTab.id,{url:searchUrl}); await sleep(700+contextRecoveryAttempts*300); continue;
        }
        const message=`INCOMPLETE: ${contextStatus} after bounded recovery requested_search_url=${searchUrl} observed=${page.page_url||''}`;
        await requiredRequest('task_progress',progressPayload(page,'',contextStatus)); await finishIncomplete(message); return;
      }
      if(scopeRecoveryAttempted&&page.page_type==='error'){await finishIncomplete(`SAFETY_STOP: ${platform} search error surface at ${page.page_url||searchUrl}`);return;}
      if(page.extraction_scope_missing){const message=`${platform} search extraction scope missing at ${page.page_url}; diagnostics=${JSON.stringify(page.extraction_diagnostics||{})}`;await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'extraction_scope_missing',message});await finishIncomplete(`SAFETY_STOP: ${message}`);return;}
      if(page.extraction_diagnostics){await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'scope_diagnostics',message:`${platform} scoped result diagnostics`,payload:page.extraction_diagnostics}).catch(()=>{});}
      const items=page.result_links||[], pageFp=fp(items);
      if(pageFp){
        const count=(fingerprintCounts.get(pageFp)||0)+1;fingerprintCounts.set(pageFp,count);
        if(count>=MAX_IDENTICAL_FINGERPRINTS){
          if(page.exhausted){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'exhausted',reason:page.exhaustion_reason||'platform end state plus repeated page fingerprint',exhausted:true});}
          else{await finishIncomplete('SAFETY_STOP: repeated stable result fingerprint without verified platform end state');}
          return;
        }
      }
      pagesVisited+=1; resultsSeen+=items.length; cardsExtracted+=items.length;
      await requiredRequest('task_progress',progressPayload(page,pageFp,contextStatus));
      lastMeaningfulAt=Date.now();

      let eligibleCards=0, recordedThisPage=0, pagePersisted=0, pagePersistenceFailed=0, pageDuplicates=0;
      for(let idx=0;idx<items.length;idx++){
        const link=items[idx]; const age=link.posted_age_days; const eligible=age==null||age<=windowDays;
        persistenceAttempted+=1;
        let saved;
        try{saved=await requiredRequest('record_result',{
          run_id:runId,task_id:taskId,source_site:platform,
          source_job_id:link.source_job_id||'',source_url:link.url||'',
          title_hint:link.title||'',company_hint:link.company||'',location_hint:link.location||'',
          posted_text:link.posted_text||'',posted_age_days:age,eligible_for_detail:eligible,card:link,
        });}
        catch(error){
          persistenceFailed+=1; pagePersistenceFailed+=1;
          const message=`card persistence failed source_job_id=${link.source_job_id||''} url=${link.url||''}: ${error.message}`;
          await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'result_persistence_failed',message,payload:{source_job_id:link.source_job_id||'',source_url:link.url||'',query:task.query_text,platform,error:String(error.message||error)}}).catch(()=>{});
          await nativeRequest('task_progress',progressPayload(page,pageFp,contextStatus)).catch(()=>{});
          await finishIncomplete(`INCOMPLETE: ${message}`); return;
        }
        persistenceSucceeded+=1; pagePersisted+=1; if(saved.duplicate){duplicateCards+=1;pageDuplicates+=1;} pendingDetails=Number(saved.pending_count??pendingDetails);
        await requiredRequest('task_progress',progressPayload(page,pageFp,contextStatus));
        lastMeaningfulAt=Date.now(); if(eligible)eligibleCards+=1;
      }
      // SQLite, not service-worker memory, owns the pending detail queue. All
      // visible cards above are committed before the first detail navigation.
      while(true){
        if(maxResults!==null&&processed>=maxResults){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'test_limit',reason:`Acceptance limit reached (${maxResults}); production has no count limit`});return;}
        const pending=await requiredRequest('next_pending_detail',{run_id:runId,task_id:taskId,worker_id:`extension-run-${runId}`}); pendingDetails=Number(pending.pending_count||0);
        if(pending.done||!pending.detail)break;
        const work=pending.detail,link={...(work.card||{}),source_job_id:work.source_job_id,url:work.source_url,title:work.title_hint,company:work.company_hint,location:work.location_hint,posted_text:work.posted_text,posted_age_days:work.posted_age_days};
        await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'navigation',message:`open detail ${link.source_job_id||link.url}`});
        if(!detailTab)detailTab=await chrome.tabs.create({windowId:searchTarget.window_id,url:'about:blank',active:false});
        await keepBackgroundTab(detailTab.id,searchTarget.window_id);
        await chrome.tabs.update(detailTab.id,{url:link.url,active:false});
        let detail;
        try{detail=await inspectTab(detailTab.id,'JOBBOT_INSPECT_DETAIL');}catch(e){detailsFailed+=1;await requiredRequest('job_error',{run_id:runId,task_id:taskId,result_id:work.result_id,message:String(e?.message||e),url:link.url});continue;}
        const standaloneDetailDiagnostics=detail.detail_diagnostics||null,standaloneDetailUrl=detail.page_url||link.url;
        let searchPaneEvidence=null;
        if(platform==='linkedin'&&!String(detail.job?.description||'').trim()){
          try{searchPaneEvidence=await inspectTab(searchTab.id,'JOBBOT_INSPECT_SEARCH_PANE',{source_job_id:link.source_job_id,select:true},4);}catch(error){searchPaneEvidence={selected:false,selection_attempted:false,error:String(error?.message||error).slice(0,300)};}
          if(searchPaneEvidence?.challenged||['challenge','login','error','interstitial'].includes(searchPaneEvidence?.page_type)){const reason=searchPaneEvidence.challenge_reason||searchPaneEvidence.surface_reason||'unsafe LinkedIn search-pane surface';await requiredRequest('detail_external_blocked',{run_id:runId,task_id:taskId,result_id:work.result_id,message:reason});await requiredRequest('pause_platform',{run_id:runId,task_id:taskId,platform,reason});return;}
          if(String(searchPaneEvidence?.job?.description||'').trim()){
            detail={...detail,page_url:searchPaneEvidence.acquisition_url||searchPaneEvidence.page_url||standaloneDetailUrl,extraction_source:searchPaneEvidence.extraction_source||'search_pane',detail_acquisition:{mode:'search_pane',url:searchPaneEvidence.acquisition_url||searchPaneEvidence.page_url||'',standalone_url:standaloneDetailUrl},standalone_detail_diagnostics:standaloneDetailDiagnostics,detail_diagnostics:searchPaneEvidence.search_pane_diagnostics||searchPaneEvidence.detail_diagnostics||detail.detail_diagnostics,job:{...detail.job,...searchPaneEvidence.job,source_job_id:link.source_job_id||detail.job?.source_job_id||searchPaneEvidence.job.source_job_id,canonical_url:detail.job?.canonical_url||searchPaneEvidence.job.canonical_url}};
          }
        }
        if(platform==='linkedin'&&(detail.detail_diagnostics||searchPaneEvidence))await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'detail_diagnostics',message:`linkedin detail extraction diagnostics ${link.source_job_id||link.url}`,payload:{source_job_id:link.source_job_id||'',requested_detail_url:link.url||'',standalone_detail:standaloneDetailDiagnostics||detail.standalone_detail_diagnostics||null,search_pane:searchPaneEvidence||null,final_extraction_source:detail.extraction_source||'none',detail_acquisition:detail.detail_acquisition||{mode:'standalone_detail',url:detail.page_url||link.url}}}).catch(()=>{});
        if(detail.challenged||detail.page_type==='challenge'){const reason=detail.challenge_reason||detail.surface_reason||'challenge on job detail';await requiredRequest('detail_external_blocked',{run_id:runId,task_id:taskId,result_id:work.result_id,message:reason});await requiredRequest('pause_platform',{run_id:runId,task_id:taskId,platform,reason});return;}
        if(detail.page_type==='login'){const reason=detail.surface_reason||'sign-in required on job detail';await requiredRequest('detail_external_blocked',{run_id:runId,task_id:taskId,result_id:work.result_id,message:reason});await requiredRequest('platform_auth_result',{run_id:runId,task_id:taskId,platform,authenticated:false,auth_state:'sign_in_required',reason,page_url:detail.page_url||link.url,requested_url:link.url,observed_url:detail.page_url||link.url});return;}
        if(detail.page_type==='error'){const reason=detail.surface_reason||'transient detail error surface';detailsFailed+=1;await requiredRequest('job_error',{run_id:runId,task_id:taskId,result_id:work.result_id,message:reason,url:detail.page_url||link.url});continue;}
        lastMeaningfulAt=Date.now();
        if(detail.job?.title&&detail.job?.canonical_url){
          await requiredRequest('detail_read',{run_id:runId,task_id:taskId,result_id:work.result_id,source_site:platform,source_job_id:link.source_job_id||detail.job?.source_job_id||'',source_url:link.url||detail.job?.canonical_url||'',detail_evidence:{page_url:detail.page_url||link.url,job:detail.job,extraction_source:detail.extraction_source||'',detail_acquisition:detail.detail_acquisition||{mode:'standalone_detail',url:detail.page_url||link.url},standalone_detail_diagnostics:standaloneDetailDiagnostics||detail.standalone_detail_diagnostics||null,detail_diagnostics:detail.detail_diagnostics||null,search_pane_evidence:searchPaneEvidence||null}}); detailRead+=1;
          if(!String(detail.job.description||'').trim()){detailsFailed+=1;await requiredRequest('job_error',{run_id:runId,task_id:taskId,result_id:work.result_id,message:'detail identity had no substantive description',url:detail.page_url||link.url,detail_diagnostics:detail.detail_diagnostics||null,search_pane_evidence:searchPaneEvidence||null});}
          else try{await requiredRequest('record_job',{run_id:runId,task_id:taskId,result_id:work.result_id,detail_evidence:detail,job:{...detail.job,search_card:link,page_url:detail.page_url||link.url}},90000);processed+=1;recordedThisPage+=1;}
          catch(error){detailsFailed+=1;const message=`record_job rejected (${detail.job.title}): ${error.message}`;if(!String(error.message||'').includes('unsafe_detail_surface'))await requiredRequest('job_error',{run_id:runId,task_id:taskId,result_id:work.result_id,message});}
        } else {const message=`detail payload incomplete type=${detail?.page_type||'none'} title=${detail?.job?.title||'none'} canonical=${detail?.job?.canonical_url||'none'} page=${detail?.page_url||'none'} source=${link.source_job_id||link.url}`;detailsFailed+=1;await requiredRequest('job_error',{run_id:runId,task_id:taskId,result_id:work.result_id,message});}
        // Do not activate the search tab; preserve the user's foreground tab.
        pendingDetails=Math.max(0,pendingDetails-1); const detailCheckpoint=progressPayload(page,pageFp,contextStatus); detailCheckpoint.checkpoint.last_job_key=link.source_job_id||link.url; detailCheckpoint.checkpoint.last_result_id=work.result_id; detailCheckpoint.checkpoint.processed=processed;
        await requiredRequest('task_progress',detailCheckpoint);
        const stopAfter=await requiredRequest('should_stop',{run_id:runId});
        if(stopAfter.stop||stopAfter.stop_after_current){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'stopped',reason:stopAfter.stop?'emergency stop requested':'stop after current job requested'});return;}
        await sleep(150);
      }
      await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'result_batch',message:`query=${task.query_text} page=${pagesVisited} extracted=${items.length} persisted=${pagePersisted} failed=${pagePersistenceFailed} duplicates=${pageDuplicates} pending=${pendingDetails} details=${detailRead} canonical=${processed}`,payload:{page:pagesVisited,...cardStats(),canonical_jobs:processed,recorded_this_page:recordedThisPage,page_persisted:pagePersisted,page_persistence_failed:pagePersistenceFailed,page_duplicates:pageDuplicates}});
      if(maxResults!==null&&processed>=maxResults){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'test_limit',reason:`Acceptance limit reached (${maxResults}); production has no count limit`});return;}
      if(items.length>0&&eligibleCards===0&&items.every(x=>x.posted_age_days!=null&&x.posted_age_days>windowDays)){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'exhausted',reason:`newest-sorted results exceeded configured ${windowDays}-day boundary`,exhausted:true});return;}
      await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'navigation',message:'advance search result batch/page'});
      const adv=await advanceSearch(searchTab.id,page);
      if(!adv.advanced){
        if(page.exhausted){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'exhausted',reason:page.exhaustion_reason||'platform reported no more results',exhausted:true});}
        else{await finishIncomplete('SAFETY_STOP: no next page/batch and no verified platform end state');}
        return;
      }
      searchUrl=normalizeSearchUrl(adv.url||page.next_url||searchUrl); await sleep(400);
    }
  }catch(e){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'failed',reason:String(e?.message||e).slice(0,700)}).catch(()=>{});}
  finally{activeTaskId=null;await closeBackgroundTarget(searchTarget,[searchTab?.id,detailTab?.id]);}
}

async function runProduction(runId,expectedBuild='',refreshId=''){
  const expected=String(expectedBuild||JOBBOT_EXTENSION_BUILD);
  if(JOBBOT_EXTENSION_BUILD!==expected)throw new Error(`loaded extension build ${JOBBOT_EXTENSION_BUILD} does not match expected ${expected}`);
  const buildSignal=await reportExtensionBuild(runId,expected,refreshId);
  requireRpcOk(buildSignal,'extension_build',{run_id:runId,expected_build:expected,refresh_id:refreshId});
  activeRunId=Number(runId); await chrome.storage.local.set({jobbot_active_run_id:activeRunId,jobbot_expected_extension_build:expected,jobbot_refresh_id:String(refreshId||'')});
  runtimeConfig=await requiredRequest('runtime_config',{},10000);
  startHeartbeat();
  try{
    await requiredRequest('begin_run',{run_id:activeRunId});
    await nativeRequest('browser_event',{run_id:activeRunId,event_type:'extension_build',message:JOBBOT_EXTENSION_BUILD,payload:{build:JOBBOT_EXTENSION_BUILD,expected_build:expected,refresh_id:refreshId}});
    const authChecked=new Map();
    while(true){
      const n=await requiredRequest('next_task',{run_id:activeRunId,worker_id:`extension-run-${activeRunId}`}); if(n.stop||n.done)break; if(!n.task)break;
      const p=String(n.task.platform||'');
      if(!authChecked.has(p)){
        try{const a=await checkAuth(p,activeRunId,n.task.task_id,n.task.search_url||''); authChecked.set(p,!!a.ready);}catch(e){authChecked.set(p,false);await requiredRequest('platform_readiness',{run_id:activeRunId,task_id:n.task.task_id,platform:p,status:'retryable',auth_state:'unknown',reason:`auth/readiness probe failed: ${e?.message||e}`,search_url:n.task.search_url||''}).catch(()=>{});}
        if(!authChecked.get(p))continue;
      }
      if(!authChecked.get(p))continue;
      await processTask(activeRunId,n.task);
    }
    const fin=await requiredRequest('finish_run',{run_id:activeRunId});
    await chrome.storage.local.set({jobbot_last_run_state:{run_id:activeRunId,status:fin.status,at:new Date().toISOString()}});
    await chrome.storage.local.remove('jobbot_active_run_id'); activeRunId=null; stopHeartbeat(); return {ok:true,status:fin.status};
  }catch(e){
    await nativeRequest('run_error',{run_id:activeRunId,message:String(e?.message||e).slice(0,700)}).catch(()=>{});
    // Keep active_run_id so the alarm can resume after a temporary bridge or Chrome failure.
    throw e;
  }
}

async function ensureResume(){
  if(runPromise)return;
  try{
    const x=await chrome.storage.local.get(['jobbot_active_run_id','jobbot_expected_extension_build','jobbot_refresh_id']);
    const rid=Number(x.jobbot_active_run_id||0); if(!rid)return;
    const expected=String(x.jobbot_expected_extension_build||JOBBOT_EXTENSION_BUILD), refreshId=String(x.jobbot_refresh_id||'');
    if(expected!==JOBBOT_EXTENSION_BUILD){
      const refresh=await requestExtensionRefresh(rid,expected,refreshId);
      if(refresh?.ok&&refresh.reload_required&&typeof chrome.runtime.reload==='function')chrome.runtime.reload();
      return;
    }
    const st=await nativeRequest('run_status',{run_id:rid}); const status=st?.run?.status;
    if(st?.ok&&!['completed','partial','stopped','failed'].includes(status)){
      runPromise=runProduction(rid,expected,refreshId).catch(()=>{}).finally(()=>{runPromise=null;});
    }
  }catch(_){}
}

chrome.runtime.onMessage.addListener((msg,_sender,sendResponse)=>{
  if(msg?.type==='JOBBOT_CONFIGURE_BRIDGE'){configureBridge(msg.port,msg.token).then(x=>sendResponse({ok:true,version:x.version,bridge:'loopback'})).catch(e=>sendResponse({ok:false,error:String(e?.message||e)}));return true;}
  if(msg?.type==='JOBBOT_REFRESH_EXTENSION'){
    requestExtensionRefresh(Number(msg.run_id||0),String(msg.expected_build||''),String(msg.refresh_id||''))
      .then(sendResponse).catch(e=>sendResponse({ok:false,error:String(e?.message||e)}));return true;
  }
  if(msg?.type==='JOBBOT_EXTENSION_REFRESH_STATUS'){
    nativeRequest('extension_refresh_status',{refresh_id:String(msg.refresh_id||'')},10000)
      .then(sendResponse).catch(e=>sendResponse({ok:false,error:String(e?.message||e)}));return true;
  }
  if(msg?.type==='JOBBOT_START_RUN'){
    const rid=Number(msg.run_id||0); if(!rid){sendResponse({ok:false,error:'missing run_id'});return false;}
    const expected=String(msg.expected_build||JOBBOT_EXTENSION_BUILD),refreshId=String(msg.refresh_id||'');
    if(expected!==JOBBOT_EXTENSION_BUILD){sendResponse({ok:false,error:'extension_build_mismatch',loaded_build:JOBBOT_EXTENSION_BUILD,expected_build:expected});return false;}
    if(runPromise){
      const active=Number(activeRunId||0);
      if(active===rid){sendResponse({ok:true,started:true,resumed:true,run_id:rid});}
      else{sendResponse({ok:false,error:'another browser run is still active',active_run_id:active,run_id:rid});}
      return false;
    }
    runPromise=runProduction(rid,expected,refreshId).catch(()=>{}).finally(()=>{runPromise=null;});
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
function ensureResumeAlarm(){chrome.alarms.create('jobbot-resume',{periodInMinutes:1});}
chrome.runtime.onStartup.addListener(()=>{ensureResumeAlarm();confirmStoredBuild().catch(()=>{});ensureResume();});
chrome.runtime.onInstalled.addListener(()=>{ensureResumeAlarm();confirmStoredBuild().catch(()=>{});ensureResume();});
chrome.alarms.onAlarm.addListener((a)=>{if(a.name==='jobbot-resume')ensureResume();});
ensureResumeAlarm();
confirmStoredBuild().catch(()=>{});
ensureResume();
