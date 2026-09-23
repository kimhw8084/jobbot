'use strict';

const assert=require('assert');
const fs=require('fs');
const vm=require('vm');

const WORKER=fs.readFileSync('extension/service_worker.js','utf8');
const SEARCH_URL='https://www.linkedin.com/jobs/search/?keywords=patient%20enrollment&location=United%20States&f_TPR=r604800&f_WT=2&start=150';
const INDEED_URL='https://www.indeed.com/jobs?q=data+quality&l=United+States';
const RECEIVER_ERROR='Could not establish connection. Receiving end does not exist.';
const CARD1={source_job_id:'L1',url:'https://www.linkedin.com/jobs/view/L1/',title:'Registry Analyst',company:'UW Health',location:'Madison, WI',posted_text:'1 day ago',posted_age_days:1};
const CARD2={source_job_id:'L2',url:'https://www.linkedin.com/jobs/view/L2/',title:'Data Quality Analyst',company:'UW Health',location:'Madison, WI',posted_text:'2 days ago',posted_age_days:2};

function safePage(platform,url,cards=[CARD1,CARD2]){return{platform,receiver_attached:true,platform_receiver_ready:true,attachment_generation:'replacement-generation',document_generation:'replacement-generation',page_type:'search',page_url:url,ready:true,authenticated:true,auth_state:'verified',login_required:false,challenged:false,extraction_scope_missing:false,result_links:cards,exhausted:true,exhaustion_reason:'fixture end state'};}
function panePage(card){return{platform:'linkedin',receiver_attached:true,platform_receiver_ready:true,attachment_generation:'replacement-generation',page_type:'job',page_url:SEARCH_URL,selected:true,search_pane:true,identity_proven:true,identity_status:'MATCH',selected_source_job_id:card.source_job_id,current_job_id:card.source_job_id,detail_acquisition:{mode:'search_pane'},job:{source_job_id:card.source_job_id,canonical_url:card.url,title:card.title,company:card.company,location:card.location,description:'A substantive pane description used by the regeneration fixture to prove detail dedupe and checkpoint continuity.'}};}

function makeWorker({mode='replacement_restores',platform='linkedin',searchUrl=SEARCH_URL}={}){
  const calls=[],events=[],listeners=[],tabs=new Map(),windows=new Map();
  const state={reloads:0,inspectFailures:0,createdWindows:0,createdTabs:0,detailIndex:0,records:new Set(),jobs:new Set(),checkpoints:[],attachments:[],removedWindows:[],removedTabs:[]};
  const searchTab={id:11,windowId:7,status:'complete',url:searchUrl,active:false,discarded:false,autoDiscardable:true,frozen:false,pendingUrl:''};tabs.set(11,searchTab);windows.set(7,{id:7,state:'minimized',focused:false,type:'normal',tabs:[searchTab]});
  const emitAttachment=(tab,generation)=>new Promise(resolve=>{state.attachments.push({tab_id:tab.id,generation});const message={type:'JOBBOT_CONTENT_SCRIPT_ATTACHED',phase:'platform_receiver_ready',platform,document_url:tab.url.split('?')[0],document_origin:new URL(tab.url).origin,document_path:new URL(tab.url).pathname,query_keys:[],attachment_generation:generation,document_generation:generation,ready_state:'interactive'};const handler=listeners[0];if(handler)handler(message,{tab:{id:tab.id,windowId:tab.windowId},frameId:0,documentId:`doc-${generation}`,url:tab.url},resolve);else resolve(null);});
  const replacementResponse=(tab,message)=>{
    if(mode==='replacement_persistent')throw new Error(RECEIVER_ERROR);
    if(mode==='replacement_arbitrary'&&message.type!=='JOBBOT_RECEIVER_READY')throw new Error('arbitrary runtime failure');
    if(message.type==='JOBBOT_RECEIVER_READY'){
      const generation=mode==='replacement_stale'?'old-generation':'replacement-generation';
      return{receiver_ready:true,receiver_attached:true,platform_receiver_ready:true,attachment_generation:generation,document_generation:generation,platform,page_url:tab.url,surface:'unknown'};
    }
    if(mode==='replacement_challenge')return{platform,page_type:'challenge',page_url:tab.url,challenged:true,challenge_reason:'fixture challenge'};
    if(mode==='replacement_login')return{platform,page_type:'login',page_url:tab.url,login_required:true,surface_reason:'fixture sign-in wall'};
    if(mode==='replacement_error')return{platform,page_type:'error',surface:'error',page_url:tab.url,surface_reason:'error loading fixture'};
    if(message.type==='JOBBOT_INSPECT_SEARCH_PANE')return panePage(state.detailIndex===1?CARD1:CARD2);
    if(message.type==='JOBBOT_ADVANCE_SEARCH')return{advanced:false,page_url:tab.url};
    return safePage(platform,tab.url);
  };
  const chrome={
    runtime:{getManifest:()=>({version_name:'3.2.2-prod-ready.672cf88.26'}),getURL:path=>`chrome-extension://jobbot/${path}`,onMessage:{addListener:handler=>listeners.push(handler)},onStartup:{addListener:()=>{}},onInstalled:{addListener:()=>{}},reload:()=>{}},
    storage:{local:{get:async()=>({jobbot_bridge_config:{port:43123,token:'x'.repeat(24)}}),set:async()=>{},remove:async()=>{}}},
    windows:{
      get:async id=>windows.get(id)||(()=>{throw new Error(`window ${id} missing`);})(),
      create:async({url})=>{state.createdWindows+=1;const windowId=100+state.createdWindows,actualUrl=mode==='replacement_wrong_origin'?'https://example.example/jobs/search':url,tab={id:100+state.createdWindows,windowId,status:'complete',url:actualUrl,active:false,discarded:false,autoDiscardable:true,frozen:false,pendingUrl:''};tabs.set(tab.id,tab);windows.set(windowId,{id:windowId,state:'minimized',focused:false,type:'normal',tabs:[tab]});if(mode==='replacement_missing')tabs.delete(tab.id);else if(mode!=='replacement_wrong_origin'&&mode!=='replacement_persistent')await emitAttachment(tab,'replacement-generation');return{id:windowId,tabs:[tab]};},
      remove:async id=>{state.removedWindows.push(id);const win=windows.get(id);for(const tab of win?.tabs||[])tabs.delete(tab.id);windows.delete(id);},
    },
    alarms:{create:()=>{},onAlarm:{addListener:()=>{}}},
    tabs:{
      get:async id=>tabs.get(id)||(()=>{throw new Error(`tab ${id} missing`);})(),
      reload:async id=>{state.reloads+=1;const tab=await chrome.tabs.get(id);tab.status='complete';if(mode==='same_target_restores')await emitAttachment(tab,'reload-generation');},
      update:async(id,details)=>{const tab=await chrome.tabs.get(id);Object.assign(tab,details);if(details.url)tab.status='complete';return tab;},
      move:async()=>{},
      create:async()=>{state.createdTabs+=1;throw new Error('standalone detail tab creation is forbidden');},
      remove:async id=>{state.removedTabs.push(id);tabs.delete(id);},
      sendMessage:async(id,message)=>{const tab=await chrome.tabs.get(id);if(id===11&&mode==='target_replaced'&&state.reloads===0){state.inspectFailures+=1;if(state.inspectFailures===2)tab.windowId=8;throw new Error(RECEIVER_ERROR);}if(id===11&&mode!=='healthy'&&(state.reloads===0||mode.startsWith('replacement_')))throw new Error(RECEIVER_ERROR);if(id!==11)return replacementResponse(tab,message);if(message.type==='JOBBOT_RECEIVER_READY')return{receiver_ready:true,receiver_attached:true,platform_receiver_ready:true,attachment_generation:'reload-generation',document_generation:'reload-generation',platform,page_url:tab.url,surface:'unknown'};return safePage(platform,tab.url);},
    },
  };
  const rpc=async(action,payload)=>{calls.push({action,payload});if(action==='browser_event'){events.push({type:payload.event_type,payload:payload.payload||{}});return{ok:true};}if(action==='task_progress'){state.checkpoints.push(payload.checkpoint);return{ok:true};}if(action==='should_stop')return{ok:true,stop:false};if(action==='record_result'){const key=`${payload.source_site}|${payload.source_job_id}|${payload.source_url}`,duplicate=state.records.has(key);state.records.add(key);return{ok:true,duplicate,pending_count:2-state.detailIndex};}if(action==='next_pending_detail'){if(state.detailIndex>=2)return{ok:true,done:true,pending_count:0};state.detailIndex+=1;const card=state.detailIndex===1?CARD1:CARD2;return{ok:true,done:false,pending_count:2-state.detailIndex,detail:{result_id:state.detailIndex,source_job_id:card.source_job_id,source_url:card.url,title_hint:card.title,company_hint:card.company,location_hint:card.location,posted_text:card.posted_text,posted_age_days:card.posted_age_days,card}};}if(action==='record_job'){state.jobs.add(payload.job.source_job_id);return{ok:true};}return{ok:true};};
  const sandbox={chrome,console,URL,URLSearchParams,AbortController,Date,Error,JSON,Map,Set,Promise,String,Number,Math,Array,Object,RegExp,TypeError,setTimeout,clearTimeout,fetch:async(_url,options)=>{const body=JSON.parse(options.body);return{ok:true,status:200,json:async()=>rpc(body.action,body)};}};
  vm.runInNewContext(`${WORKER}\nglobalThis.__inspectTab=inspectTab;globalThis.__processTask=processTask;globalThis.__tabLifecycleSnapshot=tabLifecycleSnapshot;globalThis.__ensureTargetDurability=ensureTargetDurability;globalThis.__recoveryTargetState=recoveryTargetState;`,sandbox,{filename:'extension/service_worker.js'});
  return{inspectTab:sandbox.__inspectTab,processTask:sandbox.__processTask,tabLifecycleSnapshot:sandbox.__tabLifecycleSnapshot,ensureTargetDurability:sandbox.__ensureTargetDurability,recoveryTargetState:sandbox.__recoveryTargetState,calls,events,state,tabs,windows,target:{tab:searchTab,window_id:7,owned_window:true}};
}

function recoveryContext(worker,overrides={}){return{platform:'linkedin',run_id:166,task_id:1,target:worker.target,requested_url:SEARCH_URL,receiver_ready_timeout_ms:30,idle_wait_ms:0,require_attachment_evidence:true,checkpoint:{requested_search_url:SEARCH_URL,search_url:SEARCH_URL,observed_page_url:SEARCH_URL,context_status:'verified',page_number:9,processed:4,page_fingerprint:'L0'},context_check:(_response,observed)=>new URL(observed).searchParams.get('start')===new URL(SEARCH_URL).searchParams.get('start')?'verified':'query_context_lost',...overrides};}
function outcomes(worker){return worker.events.filter(event=>event.type==='receiver_recovery').map(event=>event.payload.outcome);}

(async()=>{
  const restored=makeWorker({mode:'replacement_restores'});
  const restoredPage=await restored.inspectTab(11,'JOBBOT_INSPECT_SEARCH_EVENTUALLY',{},2,null,recoveryContext(restored));
  assert.strictEqual(restoredPage.page_type,'search');
  assert.strictEqual(restored.state.reloads,1);
  assert.strictEqual(restored.state.createdWindows,1);
  assert.deepStrictEqual(outcomes(restored).filter(x=>x.includes('regeneration')||x==='target_regenerated'),['target_regeneration_requested','target_regeneration_created','target_regenerated']);
  assert.strictEqual(restored.windows.size,1,'old target is retired after replacement readiness');
  assert.strictEqual(restored.state.createdTabs,0,'regeneration must not create standalone tabs');
  assert.strictEqual(restored.windows.get(101).focused,false,'regenerated owned window must remain unfocused');
  assert.strictEqual(restored.tabs.get(101).active,false,'regenerated search tab must remain inactive');

  const pendingTarget=makeWorker({mode:'healthy'});
  const pendingTab=pendingTarget.tabs.get(11);
  pendingTab.status='loading'; pendingTab.url=''; pendingTab.pendingUrl=SEARCH_URL;
  const pendingState=await pendingTarget.recoveryTargetState('linkedin',pendingTarget.target,11);
  assert.strictEqual(pendingState.valid,true,'loading replacement may validate its exact pending search URL');
  pendingTab.pendingUrl='https://example.example/jobs/search';
  const wrongPendingState=await pendingTarget.recoveryTargetState('linkedin',pendingTarget.target,11);
  assert.strictEqual(wrongPendingState.valid,false,'wrong-origin pending replacement is rejected');

  const replaced=makeWorker({mode:'healthy'});
  replaced.tabs.get(11).windowId=8;
  assert.strictEqual((await replaced.recoveryTargetState('linkedin',replaced.target,11)).valid,false,'a target moved to another window is rejected');

  const replacedDuringRecovery=makeWorker({mode:'target_replaced'});
  let replacedError;
  try{await replacedDuringRecovery.inspectTab(11,'JOBBOT_INSPECT_SEARCH_EVENTUALLY',{},2,null,recoveryContext(replacedDuringRecovery));}catch(error){replacedError=error;}
  assert(replacedError&&replacedError.name==='ReceiverRecoveryError');
  assert.strictEqual(outcomes(replacedDuringRecovery).at(-1),'target_unavailable');
  assert.strictEqual(replacedDuringRecovery.state.reloads,0,'a replaced target is never reloaded');
  assert.strictEqual(replacedDuringRecovery.state.createdWindows,0);

  const durable=makeWorker({mode:'replacement_restores'});
  const durableTask={task_id:1,platform:'linkedin',requested_search_url:SEARCH_URL,search_url:SEARCH_URL,receiver_ready_timeout_ms:30,checkpoint_json:JSON.stringify({search_url:SEARCH_URL,page_number:9,processed:4,page_fingerprint:'L0'})};
  await durable.processTask(166,durableTask,durable.target,'worker-166-linkedin');
  assert.strictEqual(durable.calls.filter(call=>call.action==='record_result').length,2);
  assert.strictEqual(durable.calls.filter(call=>call.action==='record_job').length,2);
  assert.strictEqual(durable.state.records.size,2);assert.strictEqual(durable.state.jobs.size,2);
  assert.strictEqual(durable.events.filter(event=>event.type==='detail_navigation').length,0);
  assert(durable.state.checkpoints.some(checkpoint=>checkpoint.page_number>1),'replacement must resume the durable page/checkpoint, not page one');
  assert.strictEqual(durable.state.reloads,1);assert.strictEqual(durable.state.createdWindows,1);assert.strictEqual(durable.windows.size,1);
  assert.strictEqual(durable.events.filter(event=>event.type==='receiver_recovery'&&event.payload.outcome==='target_regenerated').length,1);
  assert.strictEqual(durable.state.createdTabs,0);
  assert.strictEqual(durable.windows.get(101).focused,false);
  assert.strictEqual(durable.tabs.get(101).active,false);

  for(const [mode,expected] of [['replacement_persistent','target_regeneration_receiver_deadline_exhausted'],['replacement_stale','target_regeneration_receiver_deadline_exhausted'],['replacement_wrong_origin','context_abort'],['replacement_missing','target_unavailable'],['replacement_challenge','challenge_abort'],['replacement_login','login_abort'],['replacement_error','error_abort']]){
    const worker=makeWorker({mode});let error;
    try{await worker.inspectTab(11,'JOBBOT_INSPECT_SEARCH_EVENTUALLY',{},2,null,recoveryContext(worker));}catch(value){error=value;}
    assert(error&&error.name==='ReceiverRecoveryError',`${mode} must be bounded as a recovery error`);
    assert.strictEqual(outcomes(worker).at(-1),expected,`${mode} must produce ${expected}`);
    assert.strictEqual(worker.state.reloads,1,`${mode} permits one reload`);
    assert.strictEqual(worker.state.createdWindows,1,`${mode} permits one replacement`);
    assert.strictEqual(worker.state.createdWindows,1);
    assert.strictEqual(worker.windows.size,1,'failed recovery retires its uncommitted replacement');
    assert.strictEqual(worker.tabs.size,1,'failed recovery leaves one owned target tab');
    assert.strictEqual(worker.state.createdTabs,0);
  }

  const arbitrary=makeWorker({mode:'replacement_arbitrary'});let arbitraryError;
  try{await arbitrary.inspectTab(11,'JOBBOT_INSPECT_SEARCH_EVENTUALLY',{},2,null,recoveryContext(arbitrary));}catch(error){arbitraryError=error;}
  assert(arbitraryError&&arbitraryError.name!=='ReceiverRecoveryError');
  assert.strictEqual(arbitrary.state.reloads,1);assert.strictEqual(arbitrary.state.createdWindows,1);
  assert.strictEqual(arbitrary.state.createdTabs,0);
  assert.strictEqual(outcomes(arbitrary).includes('target_regenerated'),false,'arbitrary runtime errors are not receiver loss');

  const lifecycle=makeWorker({mode:'healthy'});
  const lifecycleState=await lifecycle.tabLifecycleSnapshot(11,7);
  assert.strictEqual(lifecycleState.discarded,false);assert.strictEqual(lifecycleState.auto_discardable,true);assert.strictEqual(lifecycleState.frozen,false);assert.strictEqual(lifecycleState.window_focused,false);assert.strictEqual(lifecycleState.document_generation,'');
  await lifecycle.ensureTargetDurability(lifecycle.target,'lifecycle_test');
  await lifecycle.inspectTab(11,'JOBBOT_INSPECT_SEARCH_EVENTUALLY',{},1);
  const after=await lifecycle.tabLifecycleSnapshot(11,7);
  assert.strictEqual(after.auto_discardable,false,'active owned target disables auto discard when API supports it');
  assert(after.last_successful_inspection);

  const indeed=makeWorker({mode:'healthy',platform:'indeed',searchUrl:INDEED_URL});
  const indeedPage=await indeed.inspectTab(11,'JOBBOT_INSPECT_SEARCH_EVENTUALLY',{},1);
  assert.strictEqual(indeedPage.platform,'indeed');assert.strictEqual(indeed.state.reloads,0);assert.strictEqual(indeed.state.createdWindows,0);
  console.log('CHG-166 receiver regeneration regressions passed: attachment generations, delayed/persistent loss, one reload/one replacement bounds, checkpoint and dedupe continuity, context/human/arbitrary gates, lifecycle evidence, and healthy Indeed behavior');
})().catch(error=>{console.error(error);process.exitCode=1;});
