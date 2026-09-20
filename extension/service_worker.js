'use strict';

let bridgeConfig=null, requestSeq=1, activeRunId=null, activeTasks=new Map(), workerPromises=new Map(), heartbeatTimer=null, resumeStarting=false;
const targetDiagnosticKeys=new Set();
const JOBBOT_EXTENSION_BUILD=String(chrome.runtime?.getManifest?.().version_name||'unknown');
let runtimeConfig={heartbeat_seconds:20,lease_seconds:180,watchdog_stall_seconds:180};
const MAX_IDENTICAL_FINGERPRINTS=3;
const LINKEDIN_SCOPE_RECOVERY_MAX_ATTEMPTS=2,LINKEDIN_SCOPE_REINSPECT_WAIT_MS=350,LINKEDIN_SCOPE_RELOAD_WAIT_MS=700;
const PRIMARY_PLATFORMS=['linkedin','indeed','glassdoor'];
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
async function createBackgroundTarget(url,mode='minimized_owned'){
  const requestedMode=['minimized_owned','normal_owned','inactive_existing'].includes(mode)?mode:'minimized_owned';
  if(requestedMode==='inactive_existing'){
    const tab=await chrome.tabs.create({url,active:false});
    return{tab,window_id:tab?.windowId??null,owned_window:false,mode:requestedMode,creation:'tabs.create'};
  }
  // Keep crawler pages in an owned non-foreground window. The diagnostic
  // matrix also exercises the retained minimized mode and an ordinary
  // inactive tab so lifecycle differences remain observable and bounded.
  if(typeof chrome.windows?.create==='function'){
    const win=await chrome.windows.create({url,focused:false,state:requestedMode==='minimized_owned'?'minimized':'normal',type:'normal'});
    const tab=win?.tabs?.[0];
    if(win?.id!=null&&tab?.id!=null)return{tab,window_id:win.id,owned_window:true,mode:requestedMode,creation:'windows.create'};
    if(win?.id!=null)try{await chrome.windows.remove(win.id);}catch(_){}
  }
  const tab=await chrome.tabs.create({url,active:false});
  return{tab,window_id:tab?.windowId??null,owned_window:false,mode:'inactive_existing',requested_mode:requestedMode,creation:'tabs.create'};
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
function diagnosticUrl(raw){
  try{
    const u=new URL(raw);
    for(const key of ['bridge_token','token','auth_token','access_token','refresh_token'])if(u.searchParams.has(key))u.searchParams.set(key,'<redacted>');
    return u.href;
  }catch(_){return raw||'';}
}
function inspectResponseSummary(resp){
  if(!resp||typeof resp!=='object')return{response_type:typeof resp};
  return{platform:resp.platform||'',page_type:resp.page_type||'',page_url:diagnosticUrl(resp.page_url||''),ready:resp.ready===true,authenticated:resp.authenticated===true,auth_state:resp.auth_state||'',login_required:resp.login_required===true,challenged:resp.challenged===true,extraction_scope_missing:resp.extraction_scope_missing===true,result_count:Array.isArray(resp.result_links)?resp.result_links.length:null};
}
async function tabLifecycleSnapshot(tabId,windowId){
  let tab=null,win=null;
  try{tab=await chrome.tabs.get(tabId);}catch(error){return{tab_id:tabId,window_id:windowId??null,error:String(error?.message||error)}}
  if(windowId!=null&&typeof chrome.windows?.get==='function')try{win=await chrome.windows.get(windowId);}catch(_){ }
  return{tab_id:tab?.id??tabId,window_id:tab?.windowId??windowId??null,url:diagnosticUrl(tab?.url||''),status:tab?.status||'',active:tab?.active===true,window_state:win?.state||'',window_focused:win?.focused===true,window_type:win?.type||''};
}
function platformTargetUrlMatches(platform,raw){
  try{
    const host=new URL(raw||'').hostname.toLowerCase();
    return platform==='linkedin'?(host==='linkedin.com'||host.endsWith('.linkedin.com')):platform==='indeed'?(host==='indeed.com'||host.endsWith('.indeed.com')):platform==='glassdoor'?(host==='glassdoor.com'||host.endsWith('.glassdoor.com')):false;
  }catch(_){return false;}
}
async function validatePlatformTarget(platform,target){
  if(!target?.tab?.id||target.window_id==null)return null;
  try{
    const tab=await chrome.tabs.get(target.tab.id);
    if(tab.windowId!==target.window_id||!platformTargetUrlMatches(platform,tab.url))return null;
    if(typeof chrome.windows?.get==='function')await chrome.windows.get(target.window_id);
    return{...target,tab};
  }catch(_){return null;}
}
async function reattachPlatformTarget(runId,platform){
  let state=null;
  try{state=await nativeRequest('run_status',{run_id:runId},10000);}catch(_){return{target:null,blocked:false,state:null};}
  const row=(state?.platforms||[]).find(item=>String(item.platform||'')===platform)||null;
  const blocked=!!row&&(['challenged','paused','sign_in_required'].includes(String(row.worker_status||''))||['challenged_cooldown','sign_in_required','retryable','unverified','user_action_required'].includes(String(row.readiness_state||'')));
  if(row?.window_id!=null&&row?.search_tab_id!=null){
    const target=await validatePlatformTarget(platform,{tab:{id:Number(row.search_tab_id),windowId:Number(row.window_id),url:String(row.search_tab_url||'')},window_id:Number(row.window_id),owned_window:!!row.owned_window,mode:'reattached',creation:'reattached'});
    if(target)return{target,blocked,state:row};
    await nativeRequest('browser_event',{run_id:runId,event_type:'worker_target_stale',message:`${platform} recorded Chrome target is stale; ids are diagnostic only`,payload:{platform,window_id:row.window_id,search_tab_id:row.search_tab_id,blocked}}).catch(()=>{});
  }
  return{target:null,blocked,state:row};
}
async function waitTabComplete(tabId,timeoutMs=45000){const deadline=Date.now()+timeoutMs;while(Date.now()<deadline){const tab=await chrome.tabs.get(tabId);if(tab.status==='complete')return tab;await sleep(400);}throw new Error('page load timed out');}
async function inspectTab(tabId,type='JOBBOT_INSPECT',extra={},retries=4,trace=null){
  for(let i=0;i<retries;i++){
    try{
      const tab=await waitTabComplete(tabId,trace?.wait_timeout_ms||45000);
      if(trace)trace.attempts.push({attempt:i+1,phase:'before_send',tab:await tabLifecycleSnapshot(tabId,tab?.windowId)});
      const resp=await chrome.tabs.sendMessage(tabId,{type,...extra});
      if(trace)trace.attempts.push({attempt:i+1,phase:'sendMessage',type,ok:true,response:inspectResponseSummary(resp)});
      if(resp)return resp;
      if(trace)trace.attempts.push({attempt:i+1,phase:'sendMessage',type,ok:false,error:'empty response'});
    }catch(e){
      if(trace)trace.attempts.push({attempt:i+1,phase:'sendMessage',type,ok:false,error:String(e?.message||e),tab:await tabLifecycleSnapshot(tabId,trace.window_id)});
      if(i===retries-1)throw e;
    }
    await sleep(700+i*220);
  }
  throw new Error('content script did not respond');
}
function expectedContentScriptMatch(raw){
  try{
    const u=new URL(raw),host=u.hostname.toLowerCase();
    const platform=host==='linkedin.com'||host.endsWith('.linkedin.com')?'linkedin':host==='indeed.com'||host.endsWith('.indeed.com')?'indeed':host==='glassdoor.com'||host.endsWith('.glassdoor.com')?'glassdoor':'';
    return{expected: u.protocol==='https:'&&!!platform,platform,origin:u.origin,pathname:u.pathname};
  }catch(_){return{expected:false,platform:'',origin:'',pathname:''};}
}
async function lastFocusedSnapshot(){
  if(typeof chrome.windows?.getLastFocused!=='function')return null;
  try{
    const win=await chrome.windows.getLastFocused({populate:true});
    const active=(win.tabs||[]).find(tab=>tab.active===true);
    return{window_id:win?.id??null,focused:win?.focused===true,state:win?.state||'',active_tab_id:active?.id??null,active_tab_url:diagnosticUrl(active?.url||'')};
  }catch(_){return null;}
}
async function runTargetDiagnostics(runId,taskId,platform,requestedUrl){
  const key=`${runId}:${platform}:${requestedUrl}`;
  if(targetDiagnosticKeys.has(key))return;
  targetDiagnosticKeys.add(key);
  const foregroundBefore=await lastFocusedSnapshot(),modes=['minimized_owned','normal_owned','inactive_existing'],results=[];
  for(const mode of modes){
    const trace={window_id:null,wait_timeout_ms:12000,attempts:[]}; let target=null; let closed=false;
    const result={mode,requested_url:requestedUrl,expected_content_script_match:expectedContentScriptMatch(requestedUrl)};
    try{
      target=await createBackgroundTarget(requestedUrl,mode); trace.window_id=target.window_id;
      result.creation={requested_mode:mode,effective_mode:target.mode,creation:target.creation,owned_window:target.owned_window===true,created:await tabLifecycleSnapshot(target.tab?.id,target.window_id)};
      const probes={};
      for(const type of ['JOBBOT_INSPECT_AUTH','JOBBOT_INSPECT_SEARCH_EVENTUALLY']){
        const probe={type,attempts:[]};
        try{const response=await inspectTab(target.tab.id,type,{},2,{...trace,attempts:probe.attempts});probe.ok=true;probe.response=inspectResponseSummary(response);}
        catch(error){probe.ok=false;probe.error=String(error?.message||error);}
        probes[type]=probe;
      }
      result.probes=probes; result.observed=await tabLifecycleSnapshot(target.tab?.id,target.window_id); result.redirected=result.observed.url!==requestedUrl;
    }catch(error){result.error=String(error?.message||error);result.observed=target?await tabLifecycleSnapshot(target.tab?.id,target.window_id):null;}
    finally{
      if(target){try{await closeBackgroundTarget(target);closed=true;}catch(error){result.close_error=String(error?.message||error);}}
      result.closed=closed;
      result.after_close=target?await tabLifecycleSnapshot(target.tab?.id,target.window_id):null;
    }
    results.push(result);
    await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'target_diagnostic',message:`${platform} target mode ${mode}`,payload:{platform,requested_url:requestedUrl,foreground_before:foregroundBefore,result}}).catch(()=>{});
  }
  const foregroundAfter=await lastFocusedSnapshot();
  await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'target_diagnostic_matrix',message:`${platform} background target comparison`,payload:{platform,requested_url:requestedUrl,foreground_before:foregroundBefore,foreground_after:foregroundAfter,foreground_preserved:JSON.stringify(foregroundBefore)===JSON.stringify(foregroundAfter),modes:results}}).catch(()=>{});
}
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
  heartbeatTimer=setInterval(()=>{
    if(!activeRunId)return;
    for(const [platform,state] of activeTasks.entries())nativeRequest('heartbeat',{run_id:activeRunId,platform,worker_id:state.worker_id,task_id:state.task_id||0,worker_status:state.status||'running'},10000).catch(()=>{});
  },Math.max(1,Number(runtimeConfig.heartbeat_seconds||20))*1000);
}
function stopHeartbeat(){if(heartbeatTimer)clearInterval(heartbeatTimer);heartbeatTimer=null;}

async function checkAuthLegacy(platform,runId,taskId,searchUrl){
  const url=AUTH_URLS[platform]||searchUrl;
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
      return {authenticated:false,ready:false,auth_state:authState,page:p};
    }
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
  } finally { await closeBackgroundTarget(target); }
}

async function checkAuth(platform,runId,taskId,searchTabId,searchUrl=''){
  // Preserve the pre-CHG-146 test/user-invoked probe signature while normal
  // production workers pass a numeric, persistent search tab id.
  if(typeof searchTabId==='string'&&!searchUrl)return checkAuthLegacy(platform,runId,taskId,searchTabId);
  const url=AUTH_URLS[platform]||searchUrl;
  let p;
  try{p=await inspectTab(searchTabId,'JOBBOT_INSPECT_AUTH',{},5);}
  catch(error){
    if(!authProbeReceiverFailure(error))throw error;
    p={platform,authenticated:false,auth_state:'unknown',reason:'search auth probe receiver unavailable',page_url:searchUrl||url};
  }
  const authState=String(p.auth_state|| (p.authenticated?'verified':p.login_required?'sign_in_required':'unknown'));
  const surface=await inspectTab(searchTabId,'JOBBOT_INSPECT_SEARCH_EVENTUALLY',{},4);
  const observed=surface.page_url||searchUrl||url;
  if(p.challenged||surface.challenged){
    await requiredRequest('pause_platform',{run_id:runId,task_id:taskId,platform,auth_state:authState==='verified'?'verified':'unknown',reason:p.challenge_reason||surface.challenge_reason||p.reason||'platform challenge',requested_url:searchUrl||url,observed_url:observed});
    return {authenticated:false,ready:false,auth_state:'challenged_cooldown',page:p,search_surface:surface};
  }
  if(authState==='sign_in_required'||surface.login_required){
    await requiredRequest('platform_auth_result',{run_id:runId,task_id:taskId,platform,authenticated:false,auth_state:'sign_in_required',reason:p.reason||`${platform} search surface requires sign-in`,page_url:observed,requested_url:searchUrl||url,observed_url:observed});
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

async function processTask(runId,task,workerTarget=null,workerId=''){
  const taskId=Number(task.task_id), platform=String(task.platform||''), owner=String(workerId||`extension-run-${runId}-${platform}`);
  activeTasks.set(platform,{worker_id:owner,task_id:taskId});
  const maxResults=task.max_results==null?null:Number(task.max_results), windowDays=Number(task.window_days||30);
  const cp=parseCheckpoint(task.checkpoint_json); const requestedSearchUrl=normalizeSearchUrl(task.requested_search_url||task.search_url); const checkpointSearchUrl=normalizeSearchUrl(cp.search_url||''); let searchUrl=(cp.context_status==='query_context_lost'||cp.context_status==='redirected')?requestedSearchUrl:(checkpointSearchUrl||requestedSearchUrl);
  let processed=Number(task.jobs_recorded||0), resultsSeen=Number(task.results_seen||0), pagesVisited=Number(task.pages_visited||0), detailRead=Number(task.detail_count_read||0);
  let cardsExtracted=Number(task.cards_extracted||0), persistenceAttempted=Number(task.cards_persistence_attempted||0), persistenceSucceeded=Number(task.cards_persistence_succeeded||0), persistenceFailed=Number(task.cards_persistence_failed||0), duplicateCards=Number(task.duplicate_cards||0), pendingDetails=Number(task.pending_details||0), detailsFailed=Number(task.details_failed||0);
  const fingerprintCounts=new Map(); let searchTarget=workerTarget,searchTab=workerTarget?.tab||null,detailTab=null,lastMeaningfulAt=Date.now(),contextRecoveryAttempts=Number(task.context_recovery_attempts||cp.context_recovery_attempts||0);
  const cardStats=()=>({extracted_cards:cardsExtracted,persistence_attempted:persistenceAttempted,persistence_succeeded:persistenceSucceeded,persistence_failed:persistenceFailed,duplicate_cards:duplicateCards,pending_details:pendingDetails,details_completed:detailRead,details_failed:detailsFailed});
  const progressPayload=(page,pageFp,contextStatus='verified')=>({run_id:runId,task_id:taskId,results_seen:resultsSeen,pages_visited:pagesVisited,checkpoint:{search_url:normalizeSearchUrl(page.page_url||searchUrl),requested_search_url:requestedSearchUrl,observed_page_url:normalizeSearchUrl(page.page_url||searchUrl),context_status:contextStatus,page_fingerprint:pageFp,processed,page_number:pagesVisited,scroll_generation:pagesVisited,context_recovery_attempts:contextRecoveryAttempts,card_stats:cardStats()}});
  const finishIncomplete=async(reason)=>{try{await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'incomplete',reason});}catch(_){/* preserve the original failure when the bridge is unavailable */}};
  try{
    await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'navigation',message:`open search ${searchUrl}`});
    if(!searchTarget){searchTarget=await createBackgroundTarget(searchUrl);searchTab=searchTarget.tab;}
    else if(searchTab&&searchTab.url!==searchUrl){await chrome.tabs.update(searchTab.id,{url:searchUrl,active:false});await sleep(650);}
    if(searchTarget?.window_id!=null){await keepBackgroundTab(searchTab.id,searchTarget.window_id);await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'worker_window_bound',message:`${platform} reused one owned search tab`,payload:{platform,worker_id:owner,window_id:searchTarget.window_id,search_tab_id:searchTab.id,owned_window:searchTarget.owned_window===true}}).catch(()=>{});}
    while(true){
      const watchdogMs=Math.max(1,Number(runtimeConfig.watchdog_stall_seconds||180))*1000;
      if(Date.now()-lastMeaningfulAt>watchdogMs){await finishIncomplete(`SAFETY_STOP: watchdog observed no meaningful progress for ${runtimeConfig.watchdog_stall_seconds||180} seconds`);return;}
      const stop=await requiredRequest('should_stop',{run_id:runId,platform}); if(stop.stop){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'stopped',reason:'stop requested'});return;}
      await keepBackgroundTab(searchTab.id,searchTarget.window_id);
      let scopeRecoveryAttempted=false;
      let page=await inspectTab(searchTab.id,'JOBBOT_INSPECT_SEARCH_EVENTUALLY');
      if(page.challenged){await requiredRequest('pause_platform',{run_id:runId,task_id:taskId,platform,reason:page.challenge_reason||'platform challenge',requested_url:searchUrl,observed_url:page.page_url||''});return{blocked:true};}
      if(page.login_required){await requiredRequest('platform_auth_result',{run_id:runId,task_id:taskId,platform,authenticated:false,auth_state:'sign_in_required',reason:`${platform} search surface requires sign-in`,page_url:page.page_url||'',requested_url:searchUrl,observed_url:page.page_url||''});return;}
      let contextStatus=searchContextStatus(searchUrl,page.page_url||'',platform);
      if(contextStatus==='verified'&&platform==='linkedin'&&page.extraction_scope_missing){scopeRecoveryAttempted=true;page=await recoverLinkedInSearchScope(searchTab.id,searchUrl,page,runId,taskId);contextStatus=searchContextStatus(searchUrl,page.page_url||'',platform);}
      if(page.challenged){await requiredRequest('pause_platform',{run_id:runId,task_id:taskId,platform,reason:page.challenge_reason||'platform challenge',requested_url:searchUrl,observed_url:page.page_url||''});return{blocked:true};}
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
      if(page.challenged){await requiredRequest('pause_platform',{run_id:runId,task_id:taskId,platform,reason:page.challenge_reason||'platform challenge',requested_url:searchUrl,observed_url:page.page_url||''});return{blocked:true};}
      if(page.login_required){await requiredRequest('platform_auth_result',{run_id:runId,task_id:taskId,platform,authenticated:false,auth_state:'sign_in_required',reason:`${platform} search surface requires sign-in`,page_url:page.page_url||'',requested_url:searchUrl,observed_url:page.page_url||''});return;}
      contextStatus=searchContextStatus(searchUrl,page.page_url||'',platform);
      if(contextStatus!=='verified'){const message=`INCOMPLETE: ${contextStatus} after bounded recovery requested_search_url=${searchUrl} observed=${page.page_url||''}`;await requiredRequest('task_progress',progressPayload(page,'',contextStatus));await finishIncomplete(message);return;}
      if(platform==='linkedin'&&page.extraction_scope_missing){scopeRecoveryAttempted=true;page=await recoverLinkedInSearchScope(searchTab.id,searchUrl,page,runId,taskId);}
      if(page.challenged){await requiredRequest('pause_platform',{run_id:runId,task_id:taskId,platform,reason:page.challenge_reason||'platform challenge',requested_url:searchUrl,observed_url:page.page_url||''});return{blocked:true};}
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
        const pending=await requiredRequest('next_pending_detail',{run_id:runId,task_id:taskId,worker_id:owner,platform}); pendingDetails=Number(pending.pending_count||0);
        if(pending.done||!pending.detail)break;
        const work=pending.detail,link={...(work.card||{}),source_job_id:work.source_job_id,url:work.source_url,title:work.title_hint,company:work.company_hint,location:work.location_hint,posted_text:work.posted_text,posted_age_days:work.posted_age_days};
        await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'pane_selection',message:`select search-pane result ${link.source_job_id||link.url}`,payload:{platform,source_job_id:link.source_job_id||'',search_tab_id:searchTab.id}});
        let detail=null,searchPaneEvidence=null;
        if(work.cache_observation?.job){
          detail={page_type:'job',page_url:link.url,extraction_source:'cache',detail_acquisition:{...(work.cache_observation.detail_acquisition||{}),mode:'cache'},job:{...work.cache_observation.job,source_job_id:link.source_job_id,canonical_url:link.url}};
        }else if(!workerTarget){
          // Direct processTask callers from the pre-CHG-146 test harness are
          // explicitly treated as user-invoked re-enrichment compatibility.
          // Production always supplies the persistent platform target below.
          if(!detailTab)detailTab=await chrome.tabs.create({windowId:searchTarget.window_id,url:'about:blank',active:false});
          await keepBackgroundTab(detailTab.id,searchTarget.window_id);await chrome.tabs.update(detailTab.id,{url:link.url,active:false});
          await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'detail_navigation',message:`user-invoked re-enrichment detail tab ${link.source_job_id||link.url}`,payload:{platform,source_job_id:link.source_job_id||'',mode:'user_reenrichment'}}).catch(()=>{});
          try{detail=await inspectTab(detailTab.id,'JOBBOT_INSPECT_DETAIL');}catch(error){detail={page_type:'error',surface_reason:String(error?.message||error)};}
          detail={...detail,selected:true,identity_proven:true,detail_acquisition:{mode:'user_reenrichment',url:detail?.page_url||link.url}};
        }else{
          try{searchPaneEvidence=await inspectTab(searchTab.id,'JOBBOT_INSPECT_SEARCH_PANE',{source_job_id:link.source_job_id,select:true},4);}catch(error){searchPaneEvidence={selected:false,selection_attempted:false,error:String(error?.message||error).slice(0,300)};}
          detail=searchPaneEvidence;
        }
        const expectedId=String(link.source_job_id||'');
        const observedId=String(detail?.job?.source_job_id||detail?.selected_source_job_id||detail?.current_job_id||'');
        const acquisitionMode=String(detail?.detail_acquisition?.mode||detail?.acquisition_mode||'search_pane');
        if(detail?.challenged||['challenge','login','error','interstitial'].includes(detail?.page_type)||detail?.login_required){const reason=detail?.challenge_reason||detail?.surface_reason||'unsafe search-pane surface';await requiredRequest('detail_external_blocked',{run_id:runId,task_id:taskId,result_id:work.result_id,message:reason});await requiredRequest(detail?.login_required?'platform_auth_result':'pause_platform',{run_id:runId,task_id:taskId,platform,authenticated:false,auth_state:detail?.login_required?'sign_in_required':'unknown',reason});return{blocked:!detail?.login_required};}
        if(acquisitionMode==='search_pane'&&(!detail?.selected||detail?.search_pane!==true||detail?.identity_status==='MISMATCH'||(observedId&&observedId!==expectedId))){detailsFailed+=1;await requiredRequest('job_error',{run_id:runId,task_id:taskId,result_id:work.result_id,message:`pane identity mismatch or non-pane surface expected=${expectedId} observed=${observedId||'missing'}`,detail_acquisition:{mode:'search_pane',identity_status:detail?.identity_status||'MISMATCH',surface:detail?.surface||'unknown'}});continue;}
        if(acquisitionMode!=='cache'&&(!String(detail?.job?.description||'').trim()||!detail?.identity_proven)){detailsFailed+=1;await requiredRequest('job_error',{run_id:runId,task_id:taskId,result_id:work.result_id,message:'search pane detail incomplete or identity unproven',detail_acquisition:{mode:'search_pane',identity_status:detail?.identity_status||'UNPROVEN'}});continue;}
        await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'detail_diagnostics',message:`${platform} pane detail ${link.source_job_id||link.url}`,payload:{source_job_id:expectedId,selected_source_job_id:observedId,detail_acquisition_mode:acquisitionMode,identity_proven:!!detail?.identity_proven,search_pane:detail?.search_pane_diagnostics||null}}).catch(()=>{});
        if(detail.challenged||detail.page_type==='challenge'){const reason=detail.challenge_reason||detail.surface_reason||'challenge on job detail';await requiredRequest('detail_external_blocked',{run_id:runId,task_id:taskId,result_id:work.result_id,message:reason});await requiredRequest('pause_platform',{run_id:runId,task_id:taskId,platform,reason});return{blocked:true};}
        if(detail.page_type==='login'){const reason=detail.surface_reason||'sign-in required on job detail';await requiredRequest('detail_external_blocked',{run_id:runId,task_id:taskId,result_id:work.result_id,message:reason});await requiredRequest('platform_auth_result',{run_id:runId,task_id:taskId,platform,authenticated:false,auth_state:'sign_in_required',reason,page_url:detail.page_url||link.url,requested_url:link.url,observed_url:detail.page_url||link.url});return;}
        if(detail.page_type==='error'){const reason=detail.surface_reason||'transient detail error surface';detailsFailed+=1;await requiredRequest('job_error',{run_id:runId,task_id:taskId,result_id:work.result_id,message:reason,url:detail.page_url||link.url});continue;}
        lastMeaningfulAt=Date.now();
        if(detail.job?.title&&detail.job?.canonical_url){
          const evidence={page_url:detail.page_url||link.url,job:detail.job,extraction_source:detail.extraction_source||'search_pane',detail_acquisition:detail.detail_acquisition||{mode:'search_pane',url:detail.page_url||link.url},detail_diagnostics:detail.detail_diagnostics||null,search_pane_evidence:searchPaneEvidence||null};
          await requiredRequest('detail_read',{run_id:runId,task_id:taskId,result_id:work.result_id,source_site:platform,source_job_id:link.source_job_id||detail.job?.source_job_id||'',source_url:link.url||detail.job?.canonical_url||'',detail_evidence:evidence}); detailRead+=1;
          if(!String(detail.job.description||'').trim()){detailsFailed+=1;await requiredRequest('job_error',{run_id:runId,task_id:taskId,result_id:work.result_id,message:'detail identity had no substantive description',url:detail.page_url||link.url,detail_diagnostics:detail.detail_diagnostics||null,search_pane_evidence:searchPaneEvidence||null});}
          else try{await requiredRequest('record_job',{run_id:runId,task_id:taskId,result_id:work.result_id,detail_evidence:evidence,job:{...detail.job,search_card:link,page_url:detail.page_url||link.url}},90000);processed+=1;recordedThisPage+=1;}
          catch(error){detailsFailed+=1;const message=`record_job rejected (${detail.job.title}): ${error.message}`;if(!String(error.message||'').includes('unsafe_detail_surface'))await requiredRequest('job_error',{run_id:runId,task_id:taskId,result_id:work.result_id,message});}
        } else {const message=`detail payload incomplete type=${detail?.page_type||'none'} title=${detail?.job?.title||'none'} canonical=${detail?.job?.canonical_url||'none'} page=${detail?.page_url||'none'} source=${link.source_job_id||link.url}`;detailsFailed+=1;await requiredRequest('job_error',{run_id:runId,task_id:taskId,result_id:work.result_id,message});}
        // Do not activate the search tab; preserve the user's foreground tab.
        pendingDetails=Math.max(0,pendingDetails-1); const detailCheckpoint=progressPayload(page,pageFp,contextStatus); detailCheckpoint.checkpoint.last_job_key=link.source_job_id||link.url; detailCheckpoint.checkpoint.last_result_id=work.result_id; detailCheckpoint.checkpoint.processed=processed;
        await requiredRequest('task_progress',detailCheckpoint);
        const stopAfter=await requiredRequest('should_stop',{run_id:runId,platform});
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
  finally{activeTasks.delete(platform);if(!workerTarget)await closeBackgroundTarget(searchTarget,[searchTab?.id,detailTab?.id]);}
}

const SUPERVISOR_POLL_MS=2000;
async function reportWorkerRuntime(runId,platform,workerId,status,target,message){
  const runtime=target?await tabLifecycleSnapshot(target.tab?.id,target.window_id):{};
  await nativeRequest('worker_runtime',{run_id:runId,platform,worker_id:workerId,worker_status:status,owned_window:target?.owned_window===true,window_id:target?.window_id??null,window_state:status==='terminal'?'closed':status==='challenged'||status==='paused'?'human_inspectable':runtime.window_state||'',window_focused:runtime.window_focused===true,search_tab_id:target?.tab?.id??null,search_tab_url:runtime.url||target?.tab?.url||'',chrome_available:!!target,message}).catch(()=>{});
}
async function applyWorkerControl(runId,platform,target,workerId,paused=false){
  const delivery=await nativeRequest('consume_control',{run_id:runId,platform,worker_id:workerId},10000);
  const control=delivery?.control;if(!control)return{stop:false,recheck:false};
  const action=String(control.action||'');let result={action};
  if(action==='focus_window'){
    if(target?.window_id!=null&&typeof chrome.windows?.update==='function'){
      try{await chrome.windows.update(target.window_id,{focused:true});result.focused=true;}catch(error){result.focused=false;result.reason='owned window no longer exists';}
    }else result.focused=false;
  }else if(action==='emergency_stop'){result.stopped=true;}
  else if(action==='stop_after_current'||action==='stop_all'){result.stop_after_current=true;}
  else if(action==='resume_platform'||action==='recheck'){result.recheck=true;}
  else if(action==='resume_ready_platforms'){result.ready_only=true;}
  await nativeRequest('ack_control',{run_id:runId,platform,worker_id:workerId,request_id:control.request_id,status:'ACKNOWLEDGED',result},10000);
  return{stop:action==='emergency_stop'||(paused&&(action==='stop_after_current'||action==='stop_all')),recheck:result.recheck===true,action};
}

async function runPlatformWorker(runId,platform,expectedBuild,refreshId){
  const workerId=`extension-run-${runId}-${platform}`;let target=null,authReady=false,keepTarget=false,paused=false;
  try{
    const attached=await reattachPlatformTarget(runId,platform);target=attached.target;paused=attached.blocked;
    activeTasks.set(platform,{worker_id:workerId,task_id:0,status:paused?'challenged':'running'});
    if(paused){keepTarget=true;await reportWorkerRuntime(runId,platform,workerId,'challenged',target,'platform supervisor paused; waiting for explicit human recovery control');}
    while(true){
      if(target)target=await validatePlatformTarget(platform,target);
      const control=await applyWorkerControl(runId,platform,target,workerId,paused).catch(()=>({stop:false,recheck:false}));
      if(control.stop){keepTarget=false;break;}
      if(paused){
        if(control.recheck){
          paused=false;authReady=false;activeTasks.set(platform,{worker_id:workerId,task_id:0,status:'rechecking'});
          if(!target){
            const n=await requiredRequest('next_task',{run_id:runId,platform,worker_id:workerId});
            if(n.stop){keepTarget=false;break;}
            if(n.task){
              target=await createBackgroundTarget(n.task.search_url||n.task.requested_search_url||'');
              await reportWorkerRuntime(runId,platform,workerId,'rechecking',target,'replacement owned target created for explicit recovery');
            }else{paused=true;await sleep(SUPERVISOR_POLL_MS);continue;}
          }
        }else{await sleep(SUPERVISOR_POLL_MS);continue;}
      }
      if(control.recheck)authReady=false;
      const n=await requiredRequest('next_task',{run_id:runId,platform,worker_id:workerId});
      if(n.stop||n.done||!n.task){keepTarget=false;break;}
      const task=n.task;
      if(!target){
        target=await createBackgroundTarget(task.search_url||task.requested_search_url||'');
        const runtime=await tabLifecycleSnapshot(target.tab?.id,target.window_id);
        await reportWorkerRuntime(runId,platform,workerId,'running',target,'owned Chrome window and one search tab ready');
        await nativeRequest('browser_event',{run_id:runId,task_id:task.task_id,event_type:'worker_window_created',message:`${platform} owned Chrome window ready`,payload:{platform,worker_id:workerId,window_id:target.window_id,search_tab_id:target.tab?.id,owned_window:target.owned_window===true,tab_count:1}}).catch(()=>{});
      }
      if(!authReady){
          try{const a=await checkAuth(platform,runId,task.task_id,target.tab.id,task.search_url||'');authReady=!!a.ready;if(!authReady){paused=true;keepTarget=true;activeTasks.set(platform,{worker_id:workerId,task_id:0,status:'challenged'});await reportWorkerRuntime(runId,platform,workerId,'challenged',target,'platform readiness blocked; window preserved for human inspection');continue;}}
        catch(e){authReady=false;paused=true;keepTarget=true;activeTasks.set(platform,{worker_id:workerId,task_id:0,status:'challenged'});await requiredRequest('platform_readiness',{run_id:runId,task_id:task.task_id,platform,status:'retryable',auth_state:'unknown',reason:`auth/readiness probe failed: ${e?.message||e}`,search_url:task.search_url||''}).catch(()=>{});await reportWorkerRuntime(runId,platform,workerId,'challenged',target,'readiness probe failed; waiting for explicit recovery control');continue;}
      }
      const outcome=await processTask(runId,task,target,workerId);
      activeTasks.set(platform,{worker_id:workerId,task_id:0,status:'running'});
      if(outcome?.blocked){paused=true;authReady=false;keepTarget=true;activeTasks.set(platform,{worker_id:workerId,task_id:0,status:'challenged'});await reportWorkerRuntime(runId,platform,workerId,'challenged',target,'platform challenge preserved; waiting for explicit human recovery control');}
    }
  }catch(error){
    keepTarget=true;paused=true;
    await nativeRequest('run_error',{run_id:runId,platform,worker_id:workerId,message:String(error?.message||error).slice(0,700)}).catch(()=>{});
  }finally{
    const state=activeTasks.get(platform);if(state)activeTasks.delete(platform);
    if(target&&!keepTarget)await closeBackgroundTarget(target);
    await reportWorkerRuntime(runId,platform,workerId,keepTarget?'challenged':'terminal',keepTarget?target:null,keepTarget?'platform supervisor paused; window preserved for human inspection':'worker terminal');
    await nativeRequest('browser_event',{run_id:runId,event_type:'worker_terminal',message:`${platform} worker terminal`,payload:{platform,worker_id:workerId,window_id:target?.window_id??null,search_tab_id:target?.tab?.id??null,kept_for_human:keepTarget}}).catch(()=>{});
  }
}

async function runProduction(runId,expectedBuild='',refreshId=''){
  const expected=String(expectedBuild||JOBBOT_EXTENSION_BUILD);
  if(JOBBOT_EXTENSION_BUILD!==expected)throw new Error(`loaded extension build ${JOBBOT_EXTENSION_BUILD} does not match expected ${expected}`);
  const buildSignal=await reportExtensionBuild(runId,expected,refreshId);
  requireRpcOk(buildSignal,'extension_build',{run_id:runId,expected_build:expected,refresh_id:refreshId});
  activeRunId=Number(runId); await chrome.storage.local.set({jobbot_active_run_id:activeRunId,jobbot_expected_extension_build:expected,jobbot_refresh_id:String(refreshId||'')});
  runtimeConfig=await requiredRequest('runtime_config',{},10000);startHeartbeat();
  try{
    await requiredRequest('begin_run',{run_id:activeRunId});
    await nativeRequest('browser_event',{run_id:activeRunId,event_type:'extension_build',message:JOBBOT_EXTENSION_BUILD,payload:{build:JOBBOT_EXTENSION_BUILD,expected_build:expected,refresh_id:refreshId,worker_count:3}});
    workerPromises=new Map(PRIMARY_PLATFORMS.map(platform=>[platform,runPlatformWorker(activeRunId,platform,expected,refreshId)]));
    await Promise.allSettled([...workerPromises.values()]);
    const fin=await requiredRequest('finish_run',{run_id:activeRunId});
    await chrome.storage.local.set({jobbot_last_run_state:{run_id:activeRunId,status:fin.status,at:new Date().toISOString()}});
    await chrome.storage.local.remove('jobbot_active_run_id');activeRunId=null;workerPromises.clear();stopHeartbeat();return {ok:true,status:fin.status};
  }catch(e){
    await nativeRequest('run_error',{run_id:activeRunId,message:String(e?.message||e).slice(0,700)}).catch(()=>{});
    throw e;
  }
}

async function ensureResume(){
  if(activeRunId||workerPromises.size||resumeStarting)return;
  resumeStarting=true;
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
    if(st?.ok&&!['completed','stopped','failed'].includes(status)){
      const resumed=runProduction(rid,expected,refreshId).catch(()=>{});
      resumed.finally(()=>{resumeStarting=false;}).catch(()=>{});
      return;
    }
  }catch(_){}
  resumeStarting=false;
}

chrome.runtime.onMessage.addListener((msg,_sender,sendResponse)=>{
  if(msg?.type==='JOBBOT_BOOTSTRAP_START'){
    (async()=>{
      try{
        await configureBridge(Number(msg.bridge_port||0),String(msg.bridge_token||''));
        const refresh=await requestExtensionRefresh(Number(msg.run_id||0),String(msg.expected_build||JOBBOT_EXTENSION_BUILD),String(msg.refresh_id||''));
        if(refresh?.reload_required){await chrome.storage.local.set({jobbot_expected_extension_build:String(msg.expected_build||''),jobbot_refresh_id:String(refresh.refresh_id||msg.refresh_id||''),...(Number(msg.run_id||0)?{jobbot_active_run_id:Number(msg.run_id)}:{})});if(typeof chrome.runtime.reload==='function')chrome.runtime.reload();return{ok:true,reload_required:true};}
        if(!msg.maintenance&&Number(msg.run_id||0)&&!activeRunId&&!workerPromises.size){runProduction(Number(msg.run_id),String(msg.expected_build||JOBBOT_EXTENSION_BUILD),String(msg.refresh_id||'')).catch(()=>{});}
        return{ok:true,refresh};
      }catch(error){return{ok:false,error:String(error?.message||error)};}
    })().then((value)=>{sendResponse(value);if(_sender?.tab?.id!=null)setTimeout(()=>chrome.tabs.remove(_sender.tab.id).catch(()=>{}),80);});
    return true;
  }
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
    if(activeRunId||workerPromises.size||resumeStarting){
      const active=Number(activeRunId||0);
      if(active===rid||resumeStarting){sendResponse({ok:true,started:true,resumed:true,run_id:rid});}
      else{sendResponse({ok:false,error:'another browser run is still active',active_run_id:active,run_id:rid});}
      return false;
    }
    runProduction(rid,expected,refreshId).catch(()=>{});
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
