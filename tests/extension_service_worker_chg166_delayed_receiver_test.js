'use strict';

const assert=require('assert');
const fs=require('fs');
const vm=require('vm');

const WORKER=fs.readFileSync('extension/service_worker.js','utf8');
const SEARCH_URL='https://www.linkedin.com/jobs/search/?keywords=patient%20enrollment&location=United%20States&f_TPR=r604800&f_WT=2&start=150';
const RECEIVER_ERROR='Could not establish connection. Receiving end does not exist.';
const CARD1={source_job_id:'L1',url:'https://www.linkedin.com/jobs/view/L1/',title:'Registry Analyst',company:'UW Health',location:'Madison, WI',posted_text:'1 day ago',posted_age_days:1};
const CARD2={source_job_id:'L2',url:'https://www.linkedin.com/jobs/view/L2/',title:'Data Quality Analyst',company:'UW Health',location:'Madison, WI',posted_text:'2 days ago',posted_age_days:2};
const safePage=(cards=[CARD1],url=SEARCH_URL)=>({platform:'linkedin',page_type:'search',page_url:url,ready:true,authenticated:true,auth_state:'verified',login_required:false,challenged:false,extraction_scope_missing:false,result_links:cards,exhausted:true,exhaustion_reason:'fixture end state'});
const panePage=(card)=>({platform:'linkedin',page_type:'job',page_url:card.url,selected:true,search_pane:true,identity_proven:true,identity_status:'MATCH',selected_source_job_id:card.source_job_id,current_job_id:card.source_job_id,detail_acquisition:{mode:'search_pane'},job:{source_job_id:card.source_job_id,canonical_url:card.url,title:card.title,company:card.company,location:card.location,description:'A substantive pane description used only by the deterministic fixture.'}});

function makeWorker({readinessFailures=0,persistent=false,mode='direct',afterReload='safe',arbitrary=false}={}){
  const calls=[],events=[],messages=[],tabs=new Map(),windows=new Map();
  const state={reloads:0,readinessProbes:0,createdWindows:0,createdTabs:0,detailIndex:0,records:new Set(),jobs:new Set(),checkpoint:null};
  const searchTab={id:11,windowId:7,status:'complete',url:SEARCH_URL,active:false};tabs.set(11,searchTab);windows.set(7,{id:7,state:'minimized',focused:false,type:'normal'});
  const chrome={
    runtime:{getManifest:()=>({version_name:'3.2.2-prod-ready.672cf88.24'}),getURL:path=>`chrome-extension://jobbot/${path}`,onMessage:{addListener:()=>{}},onStartup:{addListener:()=>{}},onInstalled:{addListener:()=>{}},reload:()=>{}},
    storage:{local:{get:async()=>({jobbot_bridge_config:{port:43123,token:'x'.repeat(24)}}),set:async()=>{},remove:async()=>{}}},
    windows:{get:async id=>windows.get(id)||(()=>{throw new Error(`window ${id} missing`);})(),create:async()=>{state.createdWindows+=1;throw new Error('recovery target creation is forbidden in this fixture');},remove:async id=>windows.delete(id)},
    alarms:{create:()=>{},onAlarm:{addListener:()=>{}}},
    tabs:{
      get:async id=>tabs.get(id)||(()=>{throw new Error(`tab ${id} missing`);})(),
      reload:async id=>{state.reloads+=1;const tab=tabs.get(id);if(tab)tab.status='complete';},
      update:async(id,details)=>{Object.assign(await chrome.tabs.get(id),details);return tabs.get(id);},
      create:async()=>{state.createdTabs+=1;throw new Error('recovery detail tab creation is forbidden in this fixture');},remove:async id=>tabs.delete(id),move:async()=>{},
      sendMessage:async(id,message)=>{
        const tab=await chrome.tabs.get(id);messages.push({type:message.type,url:tab.url});
        if(arbitrary)throw new Error('arbitrary runtime failure');
        if(!state.reloads){throw new Error(RECEIVER_ERROR);}
        if(message.type==='JOBBOT_RECEIVER_READY'){
          state.readinessProbes+=1;
          if(persistent||state.readinessProbes<=readinessFailures)throw new Error(RECEIVER_ERROR);
          return{receiver_ready:true,platform:'linkedin',page_url:tab.url,surface:'job'};
        }
        if(persistent)throw new Error(RECEIVER_ERROR);
        if(afterReload==='challenge')return{platform:'linkedin',page_type:'challenge',page_url:tab.url,challenged:true,challenge_reason:'fixture challenge'};
        if(afterReload==='login')return{platform:'linkedin',page_type:'login',page_url:tab.url,login_required:true,surface_reason:'fixture sign-in wall'};
        if(afterReload==='context')return safePage([CARD1],SEARCH_URL.replace('start=150','start=0'));
        if(message.type==='JOBBOT_INSPECT_SEARCH_EVENTUALLY'||message.type==='JOBBOT_SCROLL_AND_INSPECT')return safePage(mode==='persistence'?[CARD1,CARD2]:[CARD1]);
        if(message.type==='JOBBOT_INSPECT_SEARCH_PANE'){const card=state.detailIndex===1?CARD1:CARD2;return panePage(card);}
        if(message.type==='JOBBOT_ADVANCE_SEARCH')return{advanced:false,page_url:tab.url};
        return safePage();
      },
    },
  };
  const rpc=async(action,payload)=>{
    calls.push({action,payload});
    if(action==='browser_event'){events.push({type:payload.event_type,payload:payload.payload||{}});return{ok:true};}
    if(action==='task_progress'){state.checkpoint=payload.checkpoint;return{ok:true};}
    if(action==='next_pending_detail'){
      if(mode!=='persistence'||state.detailIndex>=2)return{ok:true,done:true,pending_count:0};
      state.detailIndex+=1;const card=state.detailIndex===1?CARD1:CARD2;
      return{ok:true,done:false,pending_count:2-state.detailIndex,detail:{result_id:state.detailIndex,source_job_id:card.source_job_id,source_url:card.url,title_hint:card.title,company_hint:card.company,location_hint:card.location,posted_text:card.posted_text,posted_age_days:card.posted_age_days,card}};
    }
    if(action==='record_result'){
      const key=`${payload.source_site}|${payload.source_job_id}|${payload.source_url}`;const duplicate=state.records.has(key);state.records.add(key);return{ok:true,duplicate,pending_count:mode==='persistence'?2-state.detailIndex:0};
    }
    if(action==='record_job'){state.jobs.add(payload.job.source_job_id);return{ok:true};}
    return{ok:true};
  };
  const sandbox={chrome,console,URL,URLSearchParams,AbortController,Date,Error,JSON,Map,Set,Promise,String,Number,Math,Array,Object,RegExp,TypeError,setTimeout:(fn)=>{fn();return 0;},clearTimeout:()=>{},fetch:async(_url,options)=>{const body=JSON.parse(options.body);return{ok:true,status:200,json:async()=>rpc(body.action,body)};}};
  vm.runInNewContext(`${WORKER}\nglobalThis.__inspectTab=inspectTab;globalThis.__processTask=processTask;`,sandbox,{filename:'extension/service_worker.js'});
  return{inspectTab:sandbox.__inspectTab,processTask:sandbox.__processTask,calls,events,messages,state,target:{tab:searchTab,window_id:7,owned_window:true}};
}

function context(worker,overrides={}){return{platform:'linkedin',run_id:166,task_id:1,target:worker.target,requested_url:SEARCH_URL,idle_wait_ms:0,receiver_ready_timeout_ms:40,checkpoint:{requested_search_url:SEARCH_URL,search_url:SEARCH_URL,observed_page_url:SEARCH_URL,context_status:'verified',page_number:9,processed:4,page_fingerprint:'L0'},context_check:(_response,observed)=>new URL(observed).searchParams.get('start')===new URL(SEARCH_URL).searchParams.get('start')?'verified':'query_context_lost',...overrides};}
function outcomes(worker){return worker.events.filter(event=>event.type==='receiver_recovery').map(event=>event.payload.outcome);}

async function run(){
  const delayed=makeWorker({readinessFailures:3});
  const page=await delayed.inspectTab(11,'JOBBOT_INSPECT_SEARCH_EVENTUALLY',{},4,null,context(delayed));
  assert.strictEqual(page.page_type,'search');
  assert.deepStrictEqual(outcomes(delayed),['ordinary_retry_exhaustion','reload_requested','reload_completed','post_reload_receiver_wait','receiver_ready','restored']);
  const wait=delayed.events.filter(event=>event.type==='receiver_recovery'&&event.payload.outcome==='post_reload_receiver_wait')[0].payload;
  const ready=delayed.events.filter(event=>event.type==='receiver_recovery'&&event.payload.outcome==='receiver_ready')[0].payload;
  assert.strictEqual(ready.readiness_attempts,4);
  assert(ready.readiness_elapsed_ms>=0&&ready.readiness_elapsed_ms<=40);
  assert.strictEqual(wait.windows_created_delta,0);assert.strictEqual(wait.tabs_created_delta,0);assert.strictEqual(wait.detail_page_navigations_delta,0);
  assert.strictEqual(delayed.state.reloads,1);assert.strictEqual(delayed.state.createdWindows,0);assert.strictEqual(delayed.state.createdTabs,0);

  const durable=makeWorker({readinessFailures:2,mode:'persistence'});
  await durable.processTask(166,{task_id:1,platform:'linkedin',requested_search_url:SEARCH_URL,search_url:SEARCH_URL,receiver_ready_timeout_ms:40,checkpoint_json:'{}'},durable.target,'worker-166-linkedin');
  assert.strictEqual(durable.calls.filter(call=>call.action==='record_result').length,2);
  assert.strictEqual(durable.calls.filter(call=>call.action==='record_job').length,2);
  assert.strictEqual(durable.state.records.size,2);assert.strictEqual(durable.state.jobs.size,2);
  const firstDetail=durable.calls.findIndex(call=>call.action==='browser_event'&&call.payload.event_type==='pane_selection');
  assert(durable.calls.map((call,index)=>call.action==='record_result'?index:-1).filter(index=>index>=0).every(index=>index<firstDetail));
  assert.strictEqual(outcomes(durable).at(-1),'restored');assert.strictEqual(durable.state.reloads,1);
  assert.strictEqual(durable.state.createdWindows,0);assert.strictEqual(durable.state.createdTabs,0);

  const absent=makeWorker({persistent:true});
  const result=await absent.processTask(166,{task_id:1,platform:'linkedin',requested_search_url:SEARCH_URL,search_url:SEARCH_URL,receiver_ready_timeout_ms:40,checkpoint_json:JSON.stringify({search_url:SEARCH_URL,page_number:9,processed:4,page_fingerprint:'L0'})},absent.target,'worker-166-linkedin');
  assert.strictEqual(result.system_retryable,true);
  assert.strictEqual(absent.calls.filter(call=>call.action==='platform_readiness'&&call.payload.status==='retryable').length,1);
  assert.strictEqual(absent.calls.filter(call=>call.action==='pause_platform'||call.action==='platform_auth_result').length,0);
  assert.strictEqual(absent.calls.filter(call=>call.action==='complete_task').length,1);
  assert.strictEqual(absent.state.checkpoint.page_number,9);
  assert.strictEqual(absent.state.checkpoint.processed,4);
  assert.strictEqual(absent.state.checkpoint.page_fingerprint,'L0');
  assert.strictEqual(absent.state.checkpoint.requested_search_url,SEARCH_URL.replaceAll('%20','+'));
  assert.strictEqual(absent.state.reloads,1);assert(absent.state.readinessProbes>0);assert.deepStrictEqual(outcomes(absent).at(-1),'receiver_deadline_exhausted');

  for(const surface of ['challenge','login','context']){
    const worker=makeWorker({afterReload:surface});let error;
    try{await worker.inspectTab(11,'JOBBOT_INSPECT_SEARCH_EVENTUALLY',{},4,null,context(worker));}catch(value){error=value;}
    assert(error&&error.name==='ReceiverRecoveryError');
    assert.strictEqual(outcomes(worker).at(-1),surface==='challenge'?'challenge_abort':surface==='login'?'login_abort':'context_abort');
  }
  const noRecovery=makeWorker({arbitrary:true});
  const tabs=noRecovery.target;
  let arbitraryError;
  try{await noRecovery.inspectTab(tabs.tab.id,'JOBBOT_INSPECT_SEARCH_EVENTUALLY',{},1,{window_id:7,attempts:[]},{...context(noRecovery),target:tabs});}catch(value){arbitraryError=value;}
  assert(arbitraryError&&arbitraryError.name!=='ReceiverRecoveryError');
  assert.strictEqual(noRecovery.state.reloads,0);assert.strictEqual(outcomes(noRecovery).length,0);
  console.log('CHG-166 delayed receiver regressions passed: delayed readiness, bounded deadline SYSTEM_RETRYABLE checkpoint preservation, challenge/login/context gates, card-before-pane dedupe, and zero target/navigation deltas');
}

run().catch(error=>{console.error(error);process.exitCode=1;});
