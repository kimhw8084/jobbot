'use strict';

let bridgeConfig=null, requestSeq=1, activeRunId=null, activeTaskId=null, runPromise=null, heartbeatTimer=null;
const JOBBOT_EXTENSION_BUILD=String(chrome.runtime?.getManifest?.().version_name||'unknown');
const WORKSPACE_STORAGE_KEY='jobbot_workspace';
const WORKSPACE_MARKER='jobbot_workspace=1';
let runtimeConfig={heartbeat_seconds:20,lease_seconds:180,watchdog_stall_seconds:180,primary_navigation_min_gap_ms:900,primary_dom_quiet_ms:800,primary_dom_quiet_timeout_ms:5000,primary_scroll_wait_ms:900,primary_detail_transition_min_gap_ms:900,primary_transient_retry_limit:2,primary_transient_backoff_seconds:2};
let lastPrimaryNavigationAt=0;
const MAX_IDENTICAL_FINGERPRINTS=3;
const AUTH_URLS={
  linkedin:'https://www.linkedin.com/jobs/',
  indeed:'https://www.indeed.com/',
  glassdoor:'https://www.glassdoor.com/Job/index.htm',
};
const sleep=(ms)=>new Promise(r=>setTimeout(r,ms));
async function siteRespectfulPace(kind='navigation'){
  const configured=Number(kind==='detail'?runtimeConfig.primary_detail_transition_min_gap_ms:runtimeConfig.primary_navigation_min_gap_ms)||900;
  const wait=Math.max(0,lastPrimaryNavigationAt+configured-Date.now());
  if(wait>0)await sleep(wait);
  lastPrimaryNavigationAt=Date.now();
}
async function performanceEvent(runId,taskId,operation,startedAt,extra={}){const duration=Math.max(0,Date.now()-startedAt);await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'performance',message:`${operation} ${duration}ms`,payload:{operation,duration_ms:duration,...extra}}).catch(()=>{});}

async function loadBridge(){
  if(bridgeConfig?.port&&bridgeConfig?.token)return bridgeConfig;
  const x=await chrome.storage.local.get('jobbot_bridge_config');
  const b=x.jobbot_bridge_config||null;
  if(b&&Number(b.port)>0&&String(b.token||'')){bridgeConfig={port:Number(b.port),token:String(b.token)};return bridgeConfig;}
  throw new Error('Local JobBot bridge is not configured. Start the run from the .command launcher.');
}
async function configureBridge(port,token,options={},senderTabId=null){
  const p=Number(port),t=String(token||'');
  if(!Number.isInteger(p)||p<1||p>65535||t.length<20)throw new Error('Invalid local bridge configuration');
  bridgeConfig={port:p,token:t};
  await chrome.storage.local.set({jobbot_bridge_config:bridgeConfig});
  const health=await nativeRequest('ping',{},10000);
  if(!health?.ok)throw new Error(health?.error||'Local bridge ping failed');
  runtimeConfig=await requiredRequest('runtime_config',{},10000);
  const workspace=await ensureWorkspace(senderTabId);
  if(options.dashboard_url){
    const dashboard=await createOwnedTab('dashboard',ownedDashboardUrl(String(options.dashboard_url)));
    if(dashboard.jobbot_created)await activateDashboardTab(dashboard.id,workspace.window_id);
  }
  if(options.run_id){await nativeRequest('browser_event',{run_id:Number(options.run_id),event_type:'workspace_state',message:'JobBot-owned Chrome workspace established',payload:await workspaceState()}).catch(()=>{});}
  return health;
}
async function readWorkspace(){const x=await chrome.storage.local.get([WORKSPACE_STORAGE_KEY,'jobbot_workspace_window_id','jobbot_workspace_anchor_tab_id','jobbot_dashboard_tab_id','jobbot_auth_tab_id','jobbot_search_tab_id','jobbot_detail_tab_id','workspace_generation','workspace_created_at']);return{...(x[WORKSPACE_STORAGE_KEY]||{}),window_id:x.jobbot_workspace_window_id??x[WORKSPACE_STORAGE_KEY]?.window_id,anchor_tab_id:x.jobbot_workspace_anchor_tab_id??x[WORKSPACE_STORAGE_KEY]?.anchor_tab_id,dashboard_tab_id:x.jobbot_dashboard_tab_id??x[WORKSPACE_STORAGE_KEY]?.dashboard_tab_id,auth_tab_id:x.jobbot_auth_tab_id??x[WORKSPACE_STORAGE_KEY]?.auth_tab_id,search_tab_id:x.jobbot_search_tab_id??x[WORKSPACE_STORAGE_KEY]?.search_tab_id,detail_tab_id:x.jobbot_detail_tab_id??x[WORKSPACE_STORAGE_KEY]?.detail_tab_id,workspace_generation:x.workspace_generation??x[WORKSPACE_STORAGE_KEY]?.workspace_generation,workspace_created_at:x.workspace_created_at??x[WORKSPACE_STORAGE_KEY]?.workspace_created_at};}
async function saveWorkspace(value){await chrome.storage.local.set({[WORKSPACE_STORAGE_KEY]:value,jobbot_workspace_window_id:value.window_id||null,jobbot_workspace_anchor_tab_id:value.anchor_tab_id||null,jobbot_dashboard_tab_id:value.dashboard_tab_id||null,jobbot_auth_tab_id:value.auth_tab_id||null,jobbot_search_tab_id:value.search_tab_id||null,jobbot_detail_tab_id:value.detail_tab_id||null,workspace_generation:value.workspace_generation||1,workspace_created_at:value.workspace_created_at||'',workspace_creation_method:value.workspace_creation_method||'',workspace_reused:value.workspace_reused===true,controller_original_window_id:value.controller_original_window_id||null,controller_original_window_tab_count:value.controller_original_window_tab_count||0,controller_original_window_had_non_jobbot_tabs:value.controller_original_window_had_non_jobbot_tabs===true,workspace_recreation_count:value.workspace_recreation_count||0,focus_requests_by_jobbot:value.focus_requests_by_jobbot||0});return value;}
function workspaceAnchorUrl(){return chrome.runtime.getURL(`dashboard.html?${WORKSPACE_MARKER}&jobbot_role=anchor`);}
function ownedDashboardUrl(raw){try{const u=new URL(raw);u.searchParams.set(WORKSPACE_MARKER.split('=')[0],'1');u.searchParams.set('jobbot_role','dashboard');return u.href;}catch(_){return raw;}}
function roleIds(workspace){return new Set(['anchor','dashboard','auth','search','detail'].map(role=>Number(workspace?.[`${role}_tab_id`]||0)).filter(Boolean));}
function isExtensionWorkspaceTab(tab){const url=String(tab?.url||'');const origin=chrome.runtime.getURL('');return url.startsWith(origin)&&url.includes('jobbot_workspace=1');}
function isOwnedDashboardTab(tab){try{const url=new URL(String(tab?.url||''));return url.protocol==='http:'&&url.hostname==='127.0.0.1'&&url.searchParams.get('jobbot_workspace')==='1'&&url.searchParams.get('jobbot_role')==='dashboard';}catch(_){return false;}}
function isOwnedWorkspaceTab(tab,workspace){if(!tab)return false;const id=Number(tab.id||0);if(isExtensionWorkspaceTab(tab)||isOwnedDashboardTab(tab))return true;return roleIds(workspace).has(id);}
async function populatedWindow(windowId){if(windowId==null||typeof chrome.windows?.get!=='function')return null;try{return await chrome.windows.get(Number(windowId),{populate:true});}catch(_){return null;}}
function adoptionWindowSafe(win,workspace){const tabs=Array.isArray(win?.tabs)?win.tabs:[];return tabs.length>0&&tabs.every(tab=>isOwnedWorkspaceTab(tab,workspace));}
async function workspaceWindowAudit(workspace){
  const win=await populatedWindow(workspace?.window_id); const tabs=Array.isArray(win?.tabs)?win.tabs:[];
  const roleTabWindowIds={},workerTabWindowIds={}; let ownershipViolations=0;
  for(const role of ['anchor','dashboard','auth','search','detail']){
    const id=Number(workspace?.[`${role}_tab_id`]||0); if(!id)continue;
    const tab=await validTab(id,workspace.window_id); workerTabWindowIds[role]=tab?.windowId??null;
    roleTabWindowIds[role]=tab?.windowId??null;
    if(!tab||Number(tab.windowId)!==Number(workspace.window_id))ownershipViolations+=1;
  }
  for(const role of ['anchor','dashboard'])delete workerTabWindowIds[role];
  const nonJobbotTabCount=tabs.filter(tab=>!isOwnedWorkspaceTab(tab,workspace)).length;
  return {...workspace,workspace_window_id:workspace?.window_id||null,owned_anchor_tab_id:workspace?.anchor_tab_id||null,dashboard_tab_id:workspace?.dashboard_tab_id||null,auth_tab_id:workspace?.auth_tab_id||null,search_tab_id:workspace?.search_tab_id||null,detail_tab_id:workspace?.detail_tab_id||null,role_tab_window_ids:roleTabWindowIds,worker_tab_window_ids:workerTabWindowIds,non_jobbot_tab_count:nonJobbotTabCount,ownership_violations:ownershipViolations,isolated:!!workspace?.window_id&&!!workspace?.anchor_tab_id&&ownershipViolations===0};
}
async function validTab(tabId,windowId){
  if(tabId==null||windowId==null)return null;
  try{const tab=await chrome.tabs.get(Number(tabId));return tab?.windowId===Number(windowId)?tab:null;}catch(_){return null;}
}
async function ensureWorkspace(preferredTabId=null){
  const stored=await readWorkspace();
  let windowId=Number(stored.window_id||0),anchorId=Number(stored.anchor_tab_id||0);
  let anchor=await validTab(anchorId,windowId);
  let creationMethod=stored.workspace_creation_method||'';
  let reused=!!anchor;
  let controllerOriginalWindowId=null,controllerOriginalWindowTabCount=0,controllerOriginalWindowHadNonJobbotTabs=false;
  if(!anchor){
    if(preferredTabId!=null){
      try{
        const preferred=await chrome.tabs.get(Number(preferredTabId));
        const sourceWindow=await populatedWindow(preferred?.windowId);
        const preferredWorkspaceTab=preferred?.windowId!=null&&String(preferred.url||'').startsWith(chrome.runtime.getURL('dashboard.html'));
        const safeToAdopt=preferredWorkspaceTab&&adoptionWindowSafe(sourceWindow,{...stored,anchor_tab_id:preferred.id});
        if(safeToAdopt){
          windowId=Number(preferred.windowId);anchorId=Number(preferred.id);anchor=preferred;
          reused=true;creationMethod='safe_rendezvous_adoption';
        }else if(sourceWindow&&preferred?.windowId!=null){
          controllerOriginalWindowId=Number(preferred.windowId);
          controllerOriginalWindowTabCount=Array.isArray(sourceWindow.tabs)?sourceWindow.tabs.length:0;
          controllerOriginalWindowHadNonJobbotTabs=Array.isArray(sourceWindow.tabs)&&sourceWindow.tabs.some(tab=>!isOwnedWorkspaceTab(tab,{...stored,anchor_tab_id:preferred.id}));
        }
      }catch(_){/* the rendezvous tab may have been closed before adoption */}
    }
  }
  if(!anchor){
    const extensionTabs=await chrome.tabs.query({});
    for(const existing of extensionTabs.filter(tab=>isExtensionWorkspaceTab(tab))){
      const candidateWindow=await populatedWindow(existing.windowId);
      if(adoptionWindowSafe(candidateWindow,{...stored,anchor_tab_id:existing.id})){windowId=Number(existing.windowId);anchorId=Number(existing.id);anchor=existing;reused=true;creationMethod='marker_reacquisition';break;}
    }
  }
  if(!anchor){
    if(typeof chrome.windows?.create!=='function')throw new Error('workspace_window_unavailable: Chrome windows API unavailable');
    const win=await chrome.windows.create({url:workspaceAnchorUrl(),focused:false,state:'normal',type:'normal'});
    const created=win?.tabs?.[0];
    if(win?.id==null||created?.id==null)throw new Error('workspace_window_unavailable: Chrome did not return an owned window and anchor tab');
    windowId=Number(win.id);anchorId=Number(created.id);anchor=created;creationMethod='windows.create';reused=false;
    stored.workspace_generation=Number(stored.workspace_generation||0)+1;
    stored.workspace_created_at=new Date().toISOString();
  }
  if(preferredTabId!=null&&Number(preferredTabId)!==anchorId){
    const preferred=await chrome.tabs.get(Number(preferredTabId));
    if(!preferred)throw new Error('workspace_window_unavailable: controller tab disappeared');
    if(controllerOriginalWindowId==null){
      const sourceWindow=await populatedWindow(preferred.windowId);
      controllerOriginalWindowId=Number(preferred.windowId);
      controllerOriginalWindowTabCount=Array.isArray(sourceWindow?.tabs)?sourceWindow.tabs.length:0;
      controllerOriginalWindowHadNonJobbotTabs=Array.isArray(sourceWindow?.tabs)&&sourceWindow.tabs.some(tab=>!isOwnedWorkspaceTab(tab,{...stored,anchor_tab_id:preferred.id}));
    }
    await chrome.tabs.move(Number(preferredTabId),{windowId,index:-1});
    const preferredUrl=new URL(preferred.url||chrome.runtime.getURL('dashboard.html'));
    preferredUrl.searchParams.set('jobbot_workspace','1');
    await chrome.tabs.update(Number(preferredTabId),{url:preferredUrl.href,active:false});
    if(!anchorId){anchorId=Number(preferredTabId);anchor=await chrome.tabs.get(anchorId);}
  }
  const next={...stored,window_id:windowId,anchor_tab_id:anchorId,
    workspace_anchor_url:workspaceAnchorUrl(),workspace_generation:Number(stored.workspace_generation||1),
    workspace_created_at:stored.workspace_created_at||new Date().toISOString(),
    workspace_creation_method:creationMethod||'reused_saved_workspace',workspace_reused:reused,
    controller_original_window_id:controllerOriginalWindowId||stored.controller_original_window_id||null,
    controller_original_window_tab_count:controllerOriginalWindowTabCount||stored.controller_original_window_tab_count||0,
    controller_original_window_had_non_jobbot_tabs:controllerOriginalWindowHadNonJobbotTabs||stored.controller_original_window_had_non_jobbot_tabs||false,
    workspace_recreation_count:Number(stored.workspace_recreation_count||0)+(creationMethod==='windows.create'&&stored.window_id?1:0),
    focus_requests_by_jobbot:Number(stored.focus_requests_by_jobbot||0)};
  await saveWorkspace(next);
  return{...next,anchor_tab:anchor};
}
async function createOwnedTab(role,url){
  const workspace=await ensureWorkspace();
  const stored=await readWorkspace();
  const existing=await validTab(stored[`${role}_tab_id`],workspace.window_id);
  if(existing){
    await chrome.tabs.update(existing.id,{url,active:false});
    existing.jobbot_created=false;
    return existing;
  }
  if(workspace.window_id==null)throw new Error('workspace_window_unavailable: no owned window id');
  const tab=await chrome.tabs.create({windowId:workspace.window_id,url,active:false});
  if(tab?.id==null||tab.windowId!==workspace.window_id)throw new Error('workspace_window_unavailable: owned tab creation failed');
  await saveWorkspace({...await readWorkspace(),[`${role}_tab_id`]:Number(tab.id)});tab.jobbot_created=true;
  return tab;
}
async function keepBackgroundTab(tabId,windowId){
  const tab=await chrome.tabs.get(tabId);
  if(windowId==null||tab.windowId!==windowId)throw new Error('workspace_window_unavailable: internal tab escaped owned workspace');
  await chrome.tabs.update(tabId,{active:false});
  return chrome.tabs.get(tabId);
}
async function activateDashboardTab(tabId,windowId){const tab=await chrome.tabs.get(tabId);if(!tab||Number(tab.windowId)!==Number(windowId))throw new Error('workspace_window_unavailable: dashboard escaped owned workspace');await chrome.tabs.update(tabId,{active:true});return tab;}
async function workspaceState(){return workspaceWindowAudit(await ensureWorkspace());}
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
async function requiredRequest(action,payload={},timeoutMs=60000){
  return requireRpcOk(await nativeRequest(action,payload,timeoutMs),action,payload);
}
async function waitTabComplete(tabId,timeoutMs=45000){const deadline=Date.now()+timeoutMs;while(Date.now()<deadline){const tab=await chrome.tabs.get(tabId);if(tab.status==='complete')return tab;await sleep(400);}throw new Error('page load timed out');}
async function inspectTab(tabId,type='JOBBOT_INSPECT',extra={},retries=null){const limit=Math.max(1,Number(retries??runtimeConfig.primary_transient_retry_limit??2)+1);for(let i=0;i<limit;i++){try{await waitTabComplete(tabId,45000);await chrome.tabs.sendMessage(tabId,{type:'JOBBOT_WAIT_DOM_QUIET',quiet_ms:runtimeConfig.primary_dom_quiet_ms,timeout_ms:runtimeConfig.primary_dom_quiet_timeout_ms}).catch(()=>{});const resp=await chrome.tabs.sendMessage(tabId,{type,...extra});if(resp)return resp;}catch(e){if(i===limit-1)throw e;}await sleep(Math.max(100,Number(runtimeConfig.primary_transient_backoff_seconds||2)*1000*(i+1)));}throw new Error('content script did not respond');}
async function inspectSearchScope(tabId,type='JOBBOT_INSPECT_SEARCH',extra={}){
  let page=await inspectTab(tabId,type,extra);
  if(!page?.extraction_scope_missing)return page;
  await sleep(Math.max(100,Number(runtimeConfig.primary_transient_backoff_seconds||2)*1000));
  const retry=await inspectTab(tabId,type,extra,Number(runtimeConfig.primary_transient_retry_limit||2));
  if(!retry?.extraction_scope_missing)return retry;
  page.extraction_diagnostics={...(page.extraction_diagnostics||{}),stable_retry:retry.extraction_diagnostics||{}};
  return page;
}
function fp(items){return (items||[]).map(x=>x.source_job_id||x.url).filter(Boolean).sort().join('|');}
function parseCheckpoint(raw){try{return typeof raw==='string'?JSON.parse(raw||'{}'):(raw||{});}catch(_){return {};}}
function normalizeSearchUrl(raw){try{const u=new URL(raw);if(/(^|\.)linkedin\.com$/i.test(u.hostname))u.searchParams.delete('currentJobId');return u.href;}catch(_){return raw||'';}}
function startHeartbeat(){
  if(heartbeatTimer)clearInterval(heartbeatTimer);
  heartbeatTimer=setInterval(()=>{if(activeRunId)nativeRequest('heartbeat',{run_id:activeRunId,task_id:activeTaskId||0},10000).catch(()=>{});},Math.max(1,Number(runtimeConfig.heartbeat_seconds||20))*1000);
}
function stopHeartbeat(){if(heartbeatTimer)clearInterval(heartbeatTimer);heartbeatTimer=null;}

async function checkAuth(platform,runId,taskId){
  const url=AUTH_URLS[platform]; if(!url)return {authenticated:true,page:{reason:'no auth check configured'}};
  const authStarted=Date.now(); await siteRespectfulPace('navigation');
  const tab=await createOwnedTab('auth',url);
  try{
    const p=await inspectTab(tab.id,'JOBBOT_INSPECT_AUTH');
    await performanceEvent(runId,0,'auth_navigation',authStarted,{platform});
    if(p.challenged){
      await requiredRequest('pause_platform',{run_id:runId,task_id:taskId,platform,reason:p.challenge_reason||p.reason||'platform challenge'});
      return {authenticated:false,page:p};
    }
    const authenticated=!!p.authenticated&&!p.challenged;
    await requiredRequest('platform_auth_result',{run_id:runId,task_id:taskId,platform,authenticated,reason:p.reason||p.challenge_reason||'',page_url:p.page_url||''});
    return {authenticated,page:p};
  }finally{await keepBackgroundTab(tab.id,(await ensureWorkspace()).window_id).catch(()=>{});}
}

async function gatherStableSearch(tabId,initial){
  let page=initial; const merged=new Map((page.result_links||[]).map(x=>[x.source_job_id||x.url,x])); let stable=0;
  for(let i=0;i<4&&stable<1;i++){
    const before=merged.size;
    const after=await inspectSearchScope(tabId,'JOBBOT_SCROLL_AND_INSPECT',{wait_ms:runtimeConfig.primary_scroll_wait_ms,quiet_ms:runtimeConfig.primary_dom_quiet_ms,quiet_timeout_ms:runtimeConfig.primary_dom_quiet_timeout_ms});
    if(after.challenged||after.extraction_scope_missing)return after;
    for(const x of (after.result_links||[]))merged.set(x.source_job_id||x.url,x);
    page.next_url=page.next_url||after.next_url||'';
    page.page_url=after.page_url||page.page_url;
    stable=merged.size===before?stable+1:0;
  }
  page.result_links=[...merged.values()]; return page;
}

async function advanceSearch(tabId,page){
  if(page.next_url){const next=normalizeSearchUrl(page.next_url);await siteRespectfulPace('navigation');await chrome.tabs.update(tabId,{url:next,active:false});return {advanced:true,url:next};}
  try{
    await siteRespectfulPace('navigation');
    const r=await inspectTab(tabId,'JOBBOT_ADVANCE_SEARCH',{quiet_ms:runtimeConfig.primary_dom_quiet_ms,quiet_timeout_ms:runtimeConfig.primary_dom_quiet_timeout_ms},Number(runtimeConfig.primary_transient_retry_limit||2));
    if(r?.advanced){return {advanced:true,url:normalizeSearchUrl(r.page_url||'')};}
  }catch(_){}
  return {advanced:false,url:''};
}

async function processTask(runId,task){
  const taskId=Number(task.task_id), platform=String(task.platform||'');
  activeTaskId=taskId;
  const maxResults=task.max_results==null?null:Number(task.max_results), windowDays=Number(task.window_days||30);
  const cp=parseCheckpoint(task.checkpoint_json); let searchUrl=normalizeSearchUrl(cp.search_url||task.search_url);
  let processed=Number(task.jobs_recorded||0), resultsSeen=Number(task.results_seen||0), pagesVisited=Number(task.pages_visited||0), detailRead=Number(task.detail_count_read||0);
  let cardsExtracted=Number(task.cards_extracted||0), persistenceAttempted=Number(task.cards_persistence_attempted||0), persistenceSucceeded=Number(task.cards_persistence_succeeded||0), persistenceFailed=Number(task.cards_persistence_failed||0), duplicateCards=Number(task.duplicate_cards||0), pendingDetails=Number(task.pending_details||0), detailsFailed=Number(task.details_failed||0);
  const fingerprintCounts=new Map(); let searchTab=null,detailTab=null,workspace=null,lastMeaningfulAt=Date.now(),queryStarted=Date.now();
  const cardStats=()=>({extracted_cards:cardsExtracted,persistence_attempted:persistenceAttempted,persistence_succeeded:persistenceSucceeded,persistence_failed:persistenceFailed,duplicate_cards:duplicateCards,pending_details:pendingDetails,details_completed:detailRead,details_failed:detailsFailed});
  const progressPayload=(page,pageFp)=>({run_id:runId,task_id:taskId,results_seen:resultsSeen,pages_visited:pagesVisited,checkpoint:{search_url:normalizeSearchUrl(page.page_url||searchUrl),page_fingerprint:pageFp,processed,page_number:pagesVisited,scroll_generation:pagesVisited,card_stats:cardStats()}});
  const finishIncomplete=async(reason)=>{try{await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'incomplete',reason});}catch(_){/* preserve the original failure when the bridge is unavailable */}};
  try{
    const initialStop=await requiredRequest('should_stop',{run_id:runId});
    if(initialStop.stop||initialStop.stop_after_current){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'stopped',reason:initialStop.stop?'emergency stop requested':'stop before new task requested'});return;}
    await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'navigation',message:`open search ${searchUrl}`});
    workspace=await ensureWorkspace(); await siteRespectfulPace('navigation'); searchTab=await createOwnedTab('search',searchUrl);
    while(true){
      const watchdogMs=Math.max(1,Number(runtimeConfig.watchdog_stall_seconds||180))*1000;
      if(Date.now()-lastMeaningfulAt>watchdogMs){await finishIncomplete(`SAFETY_STOP: watchdog observed no meaningful progress for ${runtimeConfig.watchdog_stall_seconds||180} seconds`);return;}
      const stop=await requiredRequest('should_stop',{run_id:runId}); if(stop.stop||stop.stop_after_current){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'stopped',reason:stop.stop?'stop requested':'stop before new result page requested'});return;}
      await keepBackgroundTab(searchTab.id,workspace.window_id);
      const searchNavigationStarted=Date.now(); let page=await inspectSearchScope(searchTab.id,'JOBBOT_INSPECT_SEARCH');
      if(page.challenged){await requiredRequest('pause_platform',{run_id:runId,task_id:taskId,platform,reason:page.challenge_reason||'platform challenge'});return;}
      if(page.login_required){await requiredRequest('platform_auth_result',{run_id:runId,task_id:taskId,platform,authenticated:false,reason:`${platform} session is no longer authenticated`,page_url:page.page_url||''});return;}
      if(page.extraction_scope_missing){const message=`${platform} search extraction scope missing at ${page.page_url}; diagnostics=${JSON.stringify(page.extraction_diagnostics||{})}`;await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'extraction_scope_missing',message});await finishIncomplete(`SAFETY_STOP: ${message}`);return;}
      await performanceEvent(runId,taskId,'search_navigation',searchNavigationStarted,{platform});
      await performanceEvent(runId,taskId,'search_ready',searchNavigationStarted,{platform});
      const searchCollectStarted=Date.now();
      page=await gatherStableSearch(searchTab.id,page);
      if(page.challenged){await requiredRequest('pause_platform',{run_id:runId,task_id:taskId,platform,reason:page.challenge_reason||'platform challenge'});return;}
      if(page.login_required){await requiredRequest('platform_auth_result',{run_id:runId,task_id:taskId,platform,authenticated:false,reason:`${platform} session is no longer authenticated`,page_url:page.page_url||''});return;}
      if(page.extraction_scope_missing){const message=`${platform} search extraction scope missing at ${page.page_url}; diagnostics=${JSON.stringify(page.extraction_diagnostics||{})}`;await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'extraction_scope_missing',message});await finishIncomplete(`SAFETY_STOP: ${message}`);return;}
      await performanceEvent(runId,taskId,'search_collect',searchCollectStarted,{platform,cards:(page.result_links||[]).length});
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
      await requiredRequest('task_progress',progressPayload(page,pageFp));
      lastMeaningfulAt=Date.now();

      let eligibleCards=0, recordedThisPage=0, pagePersisted=0, pagePersistenceFailed=0, pageDuplicates=0; const cardPersistStarted=Date.now();
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
          await nativeRequest('task_progress',progressPayload(page,pageFp)).catch(()=>{});
          await finishIncomplete(`INCOMPLETE: ${message}`); return;
        }
        persistenceSucceeded+=1; pagePersisted+=1; if(saved.duplicate){duplicateCards+=1;pageDuplicates+=1;} pendingDetails=Number(saved.pending_count??pendingDetails);
        await requiredRequest('task_progress',progressPayload(page,pageFp));
        lastMeaningfulAt=Date.now(); if(eligible)eligibleCards+=1;
      }
      await performanceEvent(runId,taskId,'card_persist',cardPersistStarted,{platform,cards:items.length});
      const stopAfterCards=await requiredRequest('should_stop',{run_id:runId});
      if(stopAfterCards.stop||stopAfterCards.stop_after_current){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'stopped',reason:stopAfterCards.stop?'emergency stop requested':'stop after current result page/batch requested'});return;}
      // SQLite, not service-worker memory, owns the pending detail queue. All
      // visible cards above are committed before the first detail navigation.
      while(true){
        if(maxResults!==null&&processed>=maxResults){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'test_limit',reason:`Acceptance limit reached (${maxResults}); production has no count limit`});return;}
        const stopBeforeDetail=await requiredRequest('should_stop',{run_id:runId});
        if(stopBeforeDetail.stop||stopBeforeDetail.stop_after_current){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'stopped',reason:stopBeforeDetail.stop?'emergency stop requested':'stop before next detail requested'});return;}
        const pending=await requiredRequest('next_pending_detail',{run_id:runId,task_id:taskId,worker_id:`extension-run-${runId}`}); pendingDetails=Number(pending.pending_count||0);
        if(pending.done||!pending.detail)break;
        const work=pending.detail,link={...(work.card||{}),source_job_id:work.source_job_id,url:work.source_url,title:work.title_hint,company:work.company_hint,location:work.location_hint,posted_text:work.posted_text,posted_age_days:work.posted_age_days};
        await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'navigation',message:`open detail ${link.source_job_id||link.url}`});
        if(!detailTab)detailTab=await createOwnedTab('detail','about:blank');
        await keepBackgroundTab(detailTab.id,workspace.window_id);
        const detailStarted=Date.now(); await siteRespectfulPace('detail');
        await chrome.tabs.update(detailTab.id,{url:link.url,active:false});
        let detail;
        try{detail=await inspectTab(detailTab.id,'JOBBOT_INSPECT_DETAIL',{quiet_ms:runtimeConfig.primary_dom_quiet_ms,timeout_ms:runtimeConfig.primary_dom_quiet_timeout_ms,hydration_timeout_ms:runtimeConfig.primary_dom_quiet_timeout_ms});}catch(e){detailsFailed+=1;await requiredRequest('job_error',{run_id:runId,task_id:taskId,result_id:work.result_id,message:String(e?.message||e),url:link.url});const stopAfterError=await requiredRequest('should_stop',{run_id:runId});if(stopAfterError.stop||stopAfterError.stop_after_current){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'stopped',reason:stopAfterError.stop?'emergency stop requested':'stop after detail error requested'});return;}continue;}
        await performanceEvent(runId,taskId,'detail_navigation',detailStarted,{platform});
        await performanceEvent(runId,taskId,'detail_ready',detailStarted,{platform});
        if(detail.challenged){const reason=detail.challenge_reason||'challenge on job detail';await requiredRequest('detail_external_blocked',{run_id:runId,task_id:taskId,result_id:work.result_id,message:reason});await requiredRequest('pause_platform',{run_id:runId,task_id:taskId,platform,reason});return;}
        lastMeaningfulAt=Date.now();
        const detailParseStarted=Date.now();
        await performanceEvent(runId,taskId,'detail_parse',detailParseStarted,{platform,complete:!!(detail.job?.title&&detail.job?.canonical_url)});
        if(detail.job?.title&&detail.job?.canonical_url){
          await requiredRequest('detail_read',{run_id:runId,task_id:taskId,result_id:work.result_id,source_site:platform,source_job_id:link.source_job_id||detail.job?.source_job_id||'',source_url:link.url||detail.job?.canonical_url||''}); detailRead+=1;
          const detailRecordStarted=Date.now(); try{await requiredRequest('record_job',{run_id:runId,task_id:taskId,result_id:work.result_id,job:{...detail.job,search_card:link,page_url:detail.page_url||link.url}},90000);await performanceEvent(runId,taskId,'detail_record',detailRecordStarted,{platform});processed+=1;recordedThisPage+=1;}
          catch(error){detailsFailed+=1;const message=`record_job rejected (${detail.job.title}): ${error.message}`;await requiredRequest('job_error',{run_id:runId,task_id:taskId,result_id:work.result_id,message});}
        } else {const message=`detail payload incomplete type=${detail?.page_type||'none'} title=${detail?.job?.title||'none'} canonical=${detail?.job?.canonical_url||'none'} page=${detail?.page_url||'none'} source=${link.source_job_id||link.url}`;detailsFailed+=1;await requiredRequest('job_error',{run_id:runId,task_id:taskId,result_id:work.result_id,message});}
        // Do not activate the search tab; preserve the user's foreground tab.
        pendingDetails=Math.max(0,pendingDetails-1); const detailCheckpoint=progressPayload(page,pageFp); detailCheckpoint.checkpoint.last_job_key=link.source_job_id||link.url; detailCheckpoint.checkpoint.last_result_id=work.result_id; detailCheckpoint.checkpoint.processed=processed;
        await requiredRequest('task_progress',detailCheckpoint);
        const stopAfter=await requiredRequest('should_stop',{run_id:runId});
        if(stopAfter.stop||stopAfter.stop_after_current){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'stopped',reason:stopAfter.stop?'emergency stop requested':'stop after current job requested'});return;}
      }
      await performanceEvent(runId,taskId,'query_total',queryStarted,{platform,cards:cardsExtracted,canonical_jobs:processed});
      queryStarted=Date.now();
      await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'result_batch',message:`query=${task.query_text} page=${pagesVisited} extracted=${items.length} persisted=${pagePersisted} failed=${pagePersistenceFailed} duplicates=${pageDuplicates} pending=${pendingDetails} details=${detailRead} canonical=${processed}`,payload:{page:pagesVisited,...cardStats(),canonical_jobs:processed,recorded_this_page:recordedThisPage,page_persisted:pagePersisted,page_persistence_failed:pagePersistenceFailed,page_duplicates:pageDuplicates}});
      const stopBeforePagination=await requiredRequest('should_stop',{run_id:runId});
      if(stopBeforePagination.stop||stopBeforePagination.stop_after_current){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'stopped',reason:stopBeforePagination.stop?'emergency stop requested':'stop before next search page requested'});return;}
      if(maxResults!==null&&processed>=maxResults){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'test_limit',reason:`Acceptance limit reached (${maxResults}); production has no count limit`});return;}
      if(items.length>0&&eligibleCards===0&&items.every(x=>x.posted_age_days!=null&&x.posted_age_days>windowDays)){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'exhausted',reason:`newest-sorted results exceeded configured ${windowDays}-day boundary`,exhausted:true});return;}
      await nativeRequest('browser_event',{run_id:runId,task_id:taskId,event_type:'navigation',message:'advance search result batch/page'});
      const adv=await advanceSearch(searchTab.id,page);
      if(!adv.advanced){
        if(page.exhausted){await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:'exhausted',reason:page.exhaustion_reason||'platform reported no more results',exhausted:true});}
        else{await finishIncomplete('SAFETY_STOP: no next page/batch and no verified platform end state');}
        return;
      }
      searchUrl=normalizeSearchUrl(adv.url||page.next_url||searchUrl);
    }
  }catch(e){const message=String(e?.message||e).slice(0,700);await requiredRequest('complete_task',{run_id:runId,task_id:taskId,status:message.includes('workspace_window_unavailable')?'incomplete':'failed',reason:message}).catch(()=>{});}
  finally{activeTaskId=null; if(searchTab&&workspace)await keepBackgroundTab(searchTab.id,workspace.window_id).catch(()=>{}); if(detailTab&&workspace)await keepBackgroundTab(detailTab.id,workspace.window_id).catch(()=>{});}
}

async function runProduction(runId){
  activeRunId=Number(runId); await chrome.storage.local.set({jobbot_active_run_id:activeRunId});
  runtimeConfig=await requiredRequest('runtime_config',{},10000);
  startHeartbeat();
  try{
    await requiredRequest('begin_run',{run_id:activeRunId});
    await nativeRequest('browser_event',{run_id:activeRunId,event_type:'extension_build',message:JOBBOT_EXTENSION_BUILD,payload:{build:JOBBOT_EXTENSION_BUILD}});
    const authChecked=new Map();
    while(true){
      const n=await requiredRequest('next_task',{run_id:activeRunId,worker_id:`extension-run-${activeRunId}`}); if(n.stop||n.done)break; if(!n.task)break;
      const p=String(n.task.platform||'');
      if(!authChecked.has(p)){
        try{const a=await checkAuth(p,activeRunId,n.task.task_id); authChecked.set(p,a.authenticated);}catch(e){authChecked.set(p,false);await requiredRequest('platform_auth_result',{run_id:activeRunId,task_id:n.task.task_id,platform:p,authenticated:false,reason:`auth probe failed: ${e?.message||e}`}).catch(()=>{});}
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
    const x=await chrome.storage.local.get('jobbot_active_run_id'); const rid=Number(x.jobbot_active_run_id||0); if(!rid)return;
    const st=await nativeRequest('run_status',{run_id:rid}); const status=st?.run?.status;
    if(st?.ok&&!['completed','partial','stopped','failed'].includes(status)){
      runPromise=runProduction(rid).catch(()=>{}).finally(()=>{runPromise=null;});
    }
  }catch(_){}
}

chrome.runtime.onMessage.addListener((msg,sender,sendResponse)=>{
  if(msg?.type==='JOBBOT_CONFIGURE_BRIDGE'){
    configureBridge(msg.port,msg.token,{dashboard_url:msg.dashboard_url||'',run_id:msg.run_id||0},sender?.tab?.id||null)
      .then(async x=>sendResponse({ok:true,version:x.version,bridge:'loopback',extension_build:JOBBOT_EXTENSION_BUILD,workspace:await workspaceState()}))
      .catch(e=>sendResponse({ok:false,error:String(e?.message||e)}));
    return true;
  }
  if(msg?.type==='JOBBOT_ADOPT_WORKSPACE'){
    ensureWorkspace(sender?.tab?.id||null).then(async workspace=>{if(msg.dashboard_url){const dashboard=await createOwnedTab('dashboard',ownedDashboardUrl(String(msg.dashboard_url)));if(dashboard.jobbot_created)await activateDashboardTab(dashboard.id,workspace.window_id);}sendResponse({ok:true,workspace:await workspaceState()});}).catch(e=>sendResponse({ok:false,error:String(e?.message||e)}));return true;
  }
  if(msg?.type==='JOBBOT_START_RUN'){
    const rid=Number(msg.run_id||0); if(!rid){sendResponse({ok:false,error:'missing run_id'});return false;}
    if(runPromise){
      const active=Number(activeRunId||0);
      if(active===rid){sendResponse({ok:true,started:true,resumed:true,run_id:rid});}
      else{sendResponse({ok:false,error:'another browser run is still active',active_run_id:active,run_id:rid});}
      return false;
    }
    runPromise=runProduction(rid).catch(()=>{}).finally(()=>{runPromise=null;});
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
chrome.windows?.onRemoved?.addListener?.((windowId)=>{readWorkspace().then(async workspace=>{if(Number(workspace.window_id||0)===Number(windowId)){await saveWorkspace({...workspace,window_id:null,anchor_tab_id:null,dashboard_tab_id:null,auth_tab_id:null,search_tab_id:null,detail_tab_id:null,workspace_reused:false});}}).catch(()=>{});});
if(typeof globalThis!=='undefined')globalThis.__JobBotWorkspaceTestHooks={ensureWorkspace,createOwnedTab,workspaceState,readWorkspace,saveWorkspace,activateDashboardTab,isOwnedWorkspaceTab};
function ensureResumeAlarm(){chrome.alarms.create('jobbot-resume',{periodInMinutes:1});}
chrome.runtime.onStartup.addListener(()=>{ensureResumeAlarm();ensureResume();});
chrome.runtime.onInstalled.addListener(()=>{ensureResumeAlarm();ensureResume();});
chrome.alarms.onAlarm.addListener((a)=>{if(a.name==='jobbot-resume')ensureResume();});
ensureResumeAlarm();
ensureResume();
