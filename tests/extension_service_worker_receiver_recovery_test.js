'use strict';

const assert=require('assert');
const fs=require('fs');
const vm=require('vm');

const WORKER=fs.readFileSync('extension/service_worker.js','utf8');
const SEARCH_URL='https://www.linkedin.com/jobs/search/?keywords=patient%20enrollment&location=United%20States&f_TPR=r604800&f_WT=2&start=150';
const RECEIVER_ERROR='Could not establish connection. Receiving end does not exist.';
const CARD1={source_job_id:'L1',url:'https://www.linkedin.com/jobs/view/L1/',title:'Registry Analyst',company:'UW Health',location:'Madison, WI',posted_text:'1 day ago',posted_age_days:1};
const CARD2={source_job_id:'L2',url:'https://www.linkedin.com/jobs/view/L2/',title:'Data Quality Analyst',company:'UW Health',location:'Madison, WI',posted_text:'2 days ago',posted_age_days:2};

function safePage(cards=[CARD1],url=SEARCH_URL){
  return{platform:'linkedin',receiver_attached:true,platform_receiver_ready:true,attachment_generation:'fixture-generation',document_generation:'fixture-generation',page_type:'search',page_url:url,ready:true,authenticated:true,auth_state:'verified',login_required:false,challenged:false,extraction_scope_missing:false,result_links:cards,exhausted:true,exhaustion_reason:'fixture end state',extraction_diagnostics:{scope_method:'fixture'}};
}
function panePage(card){
  return{platform:'linkedin',page_type:'job',page_url:card.url,selected:true,search_pane:true,identity_proven:true,identity_status:'MATCH',selected_source_job_id:card.source_job_id,current_job_id:card.source_job_id,detail_acquisition:{mode:'search_pane'},job:{source_job_id:card.source_job_id,canonical_url:card.url,title:card.title,company:card.company,location:card.location,description:'A substantive observed description that is deliberately long enough to satisfy the complete-content threshold used by the durable job ledger.'}};
}

function makeWorker({mode='direct',failure='none',responseAfterReload='safe'}={}){
  const calls=[],events=[],messages=[],tabs=new Map(),windows=new Map();
  const state={reloads:0,sendCount:0,paneMessages:0,detailIndex:0,createdWindows:0,createdTabs:0,goneOnExhaustion:failure==='gone',records:new Set(),jobs:new Set()};
  const searchTab={id:11,windowId:7,status:'complete',url:SEARCH_URL,active:false};
  tabs.set(searchTab.id,searchTab);windows.set(7,{id:7,state:'minimized',focused:false,type:'normal'});
  const receiverFailure=()=>{if(failure==='receiver'||failure==='persistent'||failure==='gone'||failure==='wrong-origin')return true;return false;};
  const responseAfterRecovery=()=>{
    if(responseAfterReload==='challenge')return{platform:'linkedin',page_type:'challenge',page_url:searchTab.url,challenged:true,challenge_reason:'LinkedIn challenge'};
    if(responseAfterReload==='login')return{platform:'linkedin',page_type:'login',page_url:searchTab.url,login_required:true,surface_reason:'sign-in required'};
    if(responseAfterReload==='context')return safePage([CARD1],SEARCH_URL.replace('start=150','start=0'));
    return safePage(mode==='persistence'?[CARD1,CARD2]:[CARD1]);
  };
  const chrome={
    runtime:{getManifest:()=>({version_name:'3.2.2-prod-ready.672cf88.25'}),getURL:path=>`chrome-extension://jobbot/${path}`,onMessage:{addListener:()=>{}},onStartup:{addListener:()=>{}},onInstalled:{addListener:()=>{}},reload:()=>{}},
    storage:{local:{get:async()=>({jobbot_bridge_config:{port:43123,token:'x'.repeat(24)}}),set:async()=>{},remove:async()=>{}}},
    windows:{
      get:async id=>windows.get(id)||(()=>{throw new Error(`window ${id} missing`);})(),
      create:async({url})=>{state.createdWindows+=1;const tab={id:99+state.createdWindows,windowId:100+state.createdWindows,status:'complete',url,active:false};tabs.set(tab.id,tab);windows.set(tab.windowId,{id:tab.windowId,tabs:[tab]});return{id:tab.windowId,tabs:[tab]};},
      remove:async id=>{windows.delete(id);},
    },
    alarms:{create:()=>{},onAlarm:{addListener:()=>{}}},
    tabs:{
      get:async id=>tabs.get(id)||(()=>{throw new Error(`tab ${id} missing`);})(),
      reload:async id=>{state.reloads+=1;if(failure==='gone-after-reload'){tabs.delete(id);return;}const tab=tabs.get(id);if(tab){tab.status='complete';}},
      update:async(id,details)=>{const tab=await chrome.tabs.get(id);Object.assign(tab,details);if(details.url)tab.status='complete';return tab;},
      move:async()=>{},
      create:async({url,windowId})=>{state.createdTabs+=1;const tab={id:200+state.createdTabs,windowId:windowId||7,status:'complete',url,active:false};tabs.set(tab.id,tab);return tab;},
      remove:async id=>{tabs.delete(id);},
      sendMessage:async(id,message)=>{
        const tab=await chrome.tabs.get(id);messages.push({type:message.type,url:tab.url});
        state.sendCount+=1;
        if(failure==='arbitrary')throw new Error('bridge callback exploded');
        if(receiverFailure()&&state.reloads===0){
          if(state.goneOnExhaustion&&state.sendCount===4)tabs.delete(id);
          if(failure==='wrong-origin'&&state.sendCount===4)tab.url='https://example.example/jobs/search';
          throw new Error(RECEIVER_ERROR);
        }
        if(failure==='persistent')throw new Error(RECEIVER_ERROR);
        if(mode==='persistence'&&message.type==='JOBBOT_INSPECT_SEARCH_PANE'&&state.detailIndex===2&&state.reloads===0){
          state.paneMessages+=1;throw new Error(RECEIVER_ERROR);
        }
        if(message.type==='JOBBOT_INSPECT_SEARCH_EVENTUALLY'||message.type==='JOBBOT_SCROLL_AND_INSPECT')return responseAfterRecovery();
        if(message.type==='JOBBOT_ADVANCE_SEARCH')return{advanced:false,page_url:tab.url};
        if(message.type==='JOBBOT_INSPECT_SEARCH_PANE')return panePage(state.detailIndex===1?CARD1:CARD2);
        return safePage(mode==='persistence'?[CARD1,CARD2]:[CARD1]);
      },
    },
  };
  const rpc=async(action,payload)=>{
    calls.push({action,payload});
    if(action==='browser_event'){events.push({type:payload.event_type,payload:payload.payload||{}});return{ok:true};}
    if(action==='should_stop')return{ok:true,stop:false};
    if(action==='task_progress')return{ok:true};
    if(action==='platform_readiness'||action==='complete_task'||action==='detail_read'||action==='job_error')return{ok:true};
    if(action==='record_result'){
      const key=`${payload.source_site}|${payload.source_job_id}|${payload.source_url}`;
      const duplicate=state.records.has(key);state.records.add(key);
      return{ok:true,duplicate,pending_count:mode==='persistence'?2-state.detailIndex:0};
    }
    if(action==='next_pending_detail'){
      if(mode!=='persistence'||state.detailIndex>=2)return{ok:true,done:true,pending_count:0};
      state.detailIndex+=1;const card=state.detailIndex===1?CARD1:CARD2;
      return{ok:true,done:false,pending_count:2-state.detailIndex+1,detail:{result_id:state.detailIndex,source_job_id:card.source_job_id,source_url:card.url,title_hint:card.title,company_hint:card.company,location_hint:card.location,posted_text:card.posted_text,posted_age_days:card.posted_age_days,card}};
    }
    if(action==='record_job'){state.jobs.add(payload.job.source_job_id);return{ok:true};}
    return{ok:true};
  };
  const sandbox={chrome,console,URL,URLSearchParams,AbortController,Date,Error,JSON,Map,Set,Promise,String,Number,Math,Array,Object,RegExp,TypeError,setTimeout:(fn)=>{fn();return 0;},clearTimeout:()=>{},fetch:async(_url,options)=>{const body=JSON.parse(options.body);return{ok:true,status:200,json:async()=>rpc(body.action,body)};}};
  vm.runInNewContext(`${WORKER}\nglobalThis.__inspectTab=inspectTab;globalThis.__processTask=processTask;`,sandbox,{filename:'extension/service_worker.js'});
  return{inspectTab:sandbox.__inspectTab,processTask:sandbox.__processTask,calls,events,messages,state,target:{tab:searchTab,window_id:7,owned_window:true}};
}

function recoveryContext(worker,overrides={}){
  return{platform:'linkedin',run_id:154,task_id:41,target:worker.target,requested_url:SEARCH_URL,idle_wait_ms:0,receiver_ready_timeout_ms:40,checkpoint:{requested_search_url:SEARCH_URL,search_url:SEARCH_URL,observed_page_url:SEARCH_URL,context_status:'verified',page_number:7,scroll_generation:7,page_fingerprint:'L1'},context_check:(_response,observed)=>{try{const r=new URL(SEARCH_URL),o=new URL(observed);return r.searchParams.get('start')===o.searchParams.get('start')?'verified':'query_context_lost';}catch(_){return'unverified';}},...overrides};
}
function recoveryEvents(worker){return worker.events.filter(event=>event.type==='receiver_recovery');}
function recoveryOutcomes(worker){return recoveryEvents(worker).map(event=>event.payload.outcome);}
async function inspectWithRecovery(worker){return worker.inspectTab(11,'JOBBOT_INSPECT_SEARCH_EVENTUALLY',{},4,null,recoveryContext(worker));}

async function run(){
  const healthy=makeWorker();
  const healthyPage=await healthy.inspectTab(11,'JOBBOT_INSPECT_SEARCH_EVENTUALLY');
  assert.strictEqual(healthyPage.page_type,'search');
  assert.strictEqual(healthy.state.reloads,0,'normal inspection must not reload');
  assert.strictEqual(recoveryEvents(healthy).length,0);

  const restored=makeWorker({failure:'receiver'});
  const restoredPage=await inspectWithRecovery(restored);
  assert.strictEqual(restoredPage.page_type,'search');
  assert.strictEqual(restored.state.reloads,1,'receiver recovery permits one same-tab reload');
  assert.strictEqual(restored.state.createdWindows,0);
  assert.strictEqual(restored.state.createdTabs,0);
  assert.deepStrictEqual(recoveryOutcomes(restored),['ordinary_retry_exhaustion','reload_requested','reload_completed','post_reload_receiver_wait','receiver_ready','restored']);
  const restoredEvidence=recoveryEvents(restored).at(-1).payload;
  assert.strictEqual(restoredEvidence.same_target,true);
  assert.strictEqual(restoredEvidence.tab_id,11);
  assert.strictEqual(restoredEvidence.window_id,7);
  assert.strictEqual(restoredEvidence.windows_created_delta,0);
  assert.strictEqual(restoredEvidence.standalone_detail_tabs_delta,0);
  assert.strictEqual(restoredEvidence.readiness_attempts,1);

  const persistent=makeWorker({failure:'persistent'});
  let persistentError;
  try{await inspectWithRecovery(persistent);}catch(error){persistentError=error;}
  assert(persistentError&&persistentError.name==='ReceiverRecoveryError');
  assert.strictEqual(persistent.state.reloads,1,'persistent receiver absence must not reload twice');
  assert.strictEqual(recoveryOutcomes(persistent).at(-1),'target_regeneration_receiver_deadline_exhausted');
  assert.strictEqual(persistent.state.createdWindows,1,'persistent receiver absence permits one bounded replacement target');
  assert.strictEqual(recoveryEvents(persistent).at(-1).payload.receiver_error,RECEIVER_ERROR);
  assert(recoveryEvents(persistent).at(-1).payload.readiness_attempts>0);
  const persistentTask=makeWorker({failure:'persistent'});
  await persistentTask.processTask(154,{task_id:41,platform:'linkedin',requested_search_url:SEARCH_URL,search_url:SEARCH_URL,receiver_ready_timeout_ms:40,checkpoint_json:'{}'},persistentTask.target,'worker-154-linkedin');
  assert.deepStrictEqual(persistentTask.calls.filter(call=>call.action==='complete_task').map(call=>call.payload.status),['incomplete']);
  assert.strictEqual(persistentTask.calls.filter(call=>call.action==='platform_readiness'&&call.payload.status==='retryable').length,1);
  assert.strictEqual(persistentTask.state.reloads,1);

  for(const responseAfterReload of ['challenge','login','context']){
    const worker=makeWorker({failure:'receiver',responseAfterReload});
    let recoveryError;
    try{await inspectWithRecovery(worker);}catch(error){recoveryError=error;}
    assert(recoveryError&&recoveryError.name==='ReceiverRecoveryError');
    assert.strictEqual(recoveryOutcomes(worker).at(-1),responseAfterReload==='challenge'?'challenge_abort':responseAfterReload==='login'?'login_abort':'context_abort');
    assert.strictEqual(worker.state.reloads,1);
  }

  const gone=makeWorker({failure:'gone'});
  let goneError;
  try{await inspectWithRecovery(gone);}catch(error){goneError=error;}
  assert(goneError&&goneError.name==='ReceiverRecoveryError');
  assert.strictEqual(gone.state.reloads,0,'a missing target must not be reloaded');
  assert.strictEqual(gone.state.createdWindows,0);
  assert.strictEqual(gone.state.createdTabs,0);
  assert.strictEqual(recoveryOutcomes(gone).at(-1),'target_unavailable');

  const wrongOrigin=makeWorker({failure:'wrong-origin'});
  let wrongOriginError;
  try{await inspectWithRecovery(wrongOrigin);}catch(error){wrongOriginError=error;}
  assert(wrongOriginError&&wrongOriginError.name==='ReceiverRecoveryError');
  assert.strictEqual(wrongOrigin.state.reloads,0,'wrong-origin tabs must not be reloaded');
  assert.strictEqual(recoveryOutcomes(wrongOrigin).at(-1),'context_abort');

  // An arbitrary bridge/runtime error is not a receiver-unavailable signal.
  const arbitraryWorker=makeWorker({failure:'arbitrary'});
  let directError;
  try{await arbitraryWorker.inspectTab(11,'JOBBOT_INSPECT_SEARCH_EVENTUALLY',{},4,{window_id:7,attempts:[]},null);}catch(error){directError=error;}
  assert(directError,'arbitrary errors must still fail');
  assert.strictEqual(arbitraryWorker.state.reloads,0,'arbitrary errors must not enter recovery');
  assert.strictEqual(recoveryEvents(arbitraryWorker).length,0);

  const durable=makeWorker({mode:'persistence'});
  await durable.processTask(154,{task_id:41,platform:'linkedin',requested_search_url:SEARCH_URL,search_url:SEARCH_URL,checkpoint_json:'{}'},durable.target,'worker-154-linkedin');
  assert.strictEqual(durable.calls.filter(call=>call.action==='record_result').length,2,'cards are persisted once before enrichment');
  assert.strictEqual(durable.calls.filter(call=>call.action==='record_job').length,2,'pane details are persisted once');
  assert.strictEqual(durable.state.records.size,2);
  assert.strictEqual(durable.state.jobs.size,2);
  assert.strictEqual(durable.calls.filter(call=>call.action==='record_result'&&call.payload.source_job_id==='L1').length,1);
  assert.strictEqual(durable.calls.filter(call=>call.action==='record_result'&&call.payload.source_job_id==='L2').length,1);
  assert.strictEqual(durable.state.reloads,1);
  assert.strictEqual(recoveryOutcomes(durable).at(-1),'restored');
  assert.strictEqual(recoveryEvents(durable).at(-1).payload.same_target,true);
  assert.strictEqual(durable.state.createdWindows,0);
  assert.strictEqual(durable.state.createdTabs,0);
  const resultIndices=durable.calls.map((call,index)=>call.action==='record_result'?index:-1).filter(index=>index>=0);
  const paneIndex=durable.calls.findIndex(call=>call.action==='browser_event'&&call.payload.event_type==='pane_selection');
  assert(resultIndices.every(index=>index<paneIndex),'all cards must be durable before pane enrichment');
  assert.deepStrictEqual(durable.calls.filter(call=>call.action==='complete_task').map(call=>call.payload.status),['exhausted']);

  console.log('Service-worker receiver recovery regressions passed: bounded same-target reload, retryable exhaustion, challenge/login/context routing, target-loss and arbitrary-error gates, one-window/one-tab evidence, and durable card-before-pane dedupe');
}

run().catch(error=>{console.error(error);process.exitCode=1;});
