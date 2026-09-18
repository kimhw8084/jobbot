'use strict';

const assert=require('assert');
const fs=require('fs');
const vm=require('vm');

const WORKER=fs.readFileSync('extension/service_worker.js','utf8')
  .replace('LINKEDIN_SCOPE_REINSPECT_WAIT_MS=350,LINKEDIN_SCOPE_RELOAD_WAIT_MS=700','LINKEDIN_SCOPE_REINSPECT_WAIT_MS=1,LINKEDIN_SCOPE_RELOAD_WAIT_MS=1');
const SEARCH_URL='https://www.linkedin.com/jobs/search/?keywords=patient+enrollment+specialist&location=United+States&f_TPR=r604800&f_WT=2&start=150';
const CARD={source_job_id:'4469227431',url:'https://www.linkedin.com/jobs/view/4469227431/',title:'Clinical Registry Analyst',company:'UW Health',location:'Madison, WI',posted_text:'1 day ago',posted_age_days:1};

function missingPage(pageUrl=SEARCH_URL){
  return{platform:'linkedin',page_type:'search',page_url:pageUrl,extraction_scope_missing:true,result_links:[],extraction_diagnostics:{reason:'no_safe_result_cluster',page_url:pageUrl}};
}
function safePage({pageUrl=SEARCH_URL,links=[CARD],exhausted=true}={}){
  return{platform:'linkedin',page_type:'search',page_url:pageUrl,extraction_scope_missing:false,result_links:links,extraction_diagnostics:{scope_method:'structural'},exhausted,exhaustion_reason:exhausted?'paged_empty_end_state':''};
}

function makeWorker({searchPages, detailSurface='success'}={}){
  const calls=[],events=[],tabsUpdated=[],state={searchIndex:0,pendingClaimed:false};
  const searchTab={id:11,windowId:7,status:'complete',url:SEARCH_URL};
  const detailTab={id:12,windowId:7,status:'complete',url:'about:blank'};
  const pages=[...(searchPages||[])];
  const chrome={
    runtime:{getManifest:()=>({version_name:'3.2.2-prod-ready.672cf88.10'}),getURL:(path)=>`chrome-extension://jobbot/${path}`,onMessage:{addListener:()=>{}},onStartup:{addListener:()=>{}},onInstalled:{addListener:()=>{}},reload:()=>{}},
    storage:{local:{
      get:async()=>({jobbot_bridge_config:{port:43123,token:'x'.repeat(24)}}),
      set:async()=>{},remove:async()=>{},
    }},
    windows:{
      create:async({url})=>{searchTab.url=url;return{id:7,tabs:[searchTab]};},
      remove:async()=>{},
    },
    alarms:{create:()=>{},onAlarm:{addListener:()=>{}}},
    tabs:{
      get:async(id)=>id===searchTab.id?searchTab:detailTab,
      move:async()=>{},
      update:async(id,details)=>{
        const tab=id===searchTab.id?searchTab:detailTab;
        if(details.url){tab.url=details.url;tab.status='complete';tabsUpdated.push({id,url:details.url});}
        return tab;
      },
      create:async({url,windowId})=>{detailTab.url=url;detailTab.windowId=windowId;return detailTab;},
      remove:async()=>{},
      sendMessage:async(id,message)=>{
        if(id===searchTab.id){
          if(message.type==='JOBBOT_ADVANCE_SEARCH')return{advanced:false,page_url:searchTab.url};
          if(message.type==='JOBBOT_SCROLL_AND_INSPECT')return safePage({pageUrl:searchTab.url,links:pages[Math.min(state.searchIndex,pages.length-1)]?.result_links||[CARD],exhausted:true});
          const page=pages[Math.min(state.searchIndex++,pages.length-1)];
          return page||safePage({pageUrl:searchTab.url,links:[],exhausted:true});
        }
        if(message.type==='JOBBOT_INSPECT_DETAIL'){
          if(detailSurface==='challenge')return{platform:'linkedin',page_type:'challenge',challenged:true,challenge_reason:'challenge'};
          if(detailSurface==='login')return{platform:'linkedin',page_type:'login',surface_reason:'sign-in required'};
          if(detailSurface==='error')return{platform:'linkedin',page_type:'error',surface_reason:'error loading'};
          return{platform:'linkedin',page_type:'job',page_url:detailTab.url,job:{source_job_id:CARD.source_job_id,canonical_url:CARD.url,title:CARD.title,company:CARD.company,location:CARD.location,description:'A substantive clinical registry analyst description with more than enough detail for enrichment.'},detail_diagnostics:{route:'standalone_detail'}};
        }
        return safePage({pageUrl:searchTab.url,links:[],exhausted:true});
      },
    },
  };
  const rpc=async(action,payload)=>{
    calls.push({action,payload});
    if(action==='browser_event'){events.push({type:payload.event_type,message:payload.message,payload:payload.payload||null});return{ok:true};}
    if(action==='should_stop')return{ok:true,stop:false};
    if(action==='task_progress')return{ok:true};
    if(action==='record_result')return{ok:true,duplicate:false,pending_count:1};
    if(action==='next_pending_detail'){
      if(state.pendingClaimed)return{ok:true,done:true,pending_count:0};
      state.pendingClaimed=true;
      return{ok:true,done:false,pending_count:1,detail:{result_id:91,source_job_id:CARD.source_job_id,source_url:CARD.url,title_hint:CARD.title,company_hint:CARD.company,location_hint:CARD.location,posted_text:CARD.posted_text,posted_age_days:1,card:CARD}};
    }
    if(action==='record_job'||action==='detail_read')return{ok:true};
    if(action==='complete_task')return{ok:true};
    if(action==='platform_auth_result'||action==='pause_platform'||action==='job_error'||action==='detail_external_blocked')return{ok:true};
    return{ok:true};
  };
  const sandbox={chrome,console,URL,URLSearchParams,AbortController,Date,Error,JSON,Map,Set,Promise,String,Number,Math,Array,Object,RegExp,TypeError,setTimeout,clearTimeout,fetch:async(_url,options)=>{const body=JSON.parse(options.body);return{ok:true,status:200,json:async()=>rpc(body.action,body)};}};
  vm.runInNewContext(`${WORKER}\nglobalThis.__processTask=processTask;`,sandbox,{filename:'extension/service_worker.js'});
  return{processTask:sandbox.__processTask,calls,events,tabsUpdated};
}

async function runScenario(searchPages,detailSurface='success'){
  const worker=makeWorker({searchPages,detailSurface});
  await worker.processTask(127,{task_id:17,platform:'linkedin',requested_search_url:SEARCH_URL,search_url:SEARCH_URL,window_days:30,checkpoint_json:'{}'});
  return worker;
}

function actions(worker,name){return worker.calls.filter((call)=>call.action===name);}

(async()=>{
  const recovered=await runScenario([missingPage(),safePage(),safePage()]);
  const recoveries=recovered.events.filter((event)=>event.type==='search_scope_recovery');
  assert.deepStrictEqual(recoveries.map((event)=>event.payload.outcome),['scope_restored']);
  assert.strictEqual(recoveries[0].payload.attempt,1);
  assert.strictEqual(actions(recovered,'record_result').length,1,'recovery must not duplicate result sightings');
  assert.strictEqual(actions(recovered,'next_pending_detail').length,2,'pending detail must be claimed once and then drained');
  assert.strictEqual(actions(recovered,'detail_read').length,1,'detail event must be emitted once');
  assert.strictEqual(actions(recovered,'record_job').length,1,'canonical job must be recorded once');
  assert.deepStrictEqual(actions(recovered,'complete_task').map((call)=>call.payload.status),['exhausted']);

  const boundedStop=await runScenario([missingPage(),missingPage(),missingPage()]);
  assert.strictEqual(boundedStop.tabsUpdated.filter((update)=>update.id===11&&update.url===SEARCH_URL).length,1,'scope recovery permits one exact same-URL reload');
  assert.strictEqual(actions(boundedStop,'record_result').length,0);
  assert.deepStrictEqual(actions(boundedStop,'complete_task').map((call)=>call.payload.status),['incomplete']);
  assert.strictEqual(boundedStop.events.filter((event)=>event.type==='extraction_scope_missing').length,1,'terminal scope failure is emitted once');
  assert.deepStrictEqual(boundedStop.events.filter((event)=>event.type==='search_scope_recovery').map((event)=>event.payload.attempt),[1,2]);

  const lostDuringRecovery=await runScenario([missingPage(),missingPage(SEARCH_URL.replace('start=150','start=175')),safePage({links:[],exhausted:true}),safePage({links:[],exhausted:true})]);
  assert.strictEqual(lostDuringRecovery.events.filter((event)=>event.type==='search_scope_recovery')[0].payload.outcome,'query_context_lost');
  assert.strictEqual(lostDuringRecovery.events.filter((event)=>event.type==='search_context_recovery').length,1,'context loss must enter existing context recovery');
  assert.strictEqual(lostDuringRecovery.events.filter((event)=>event.type==='extraction_scope_missing').length,0);
  assert.deepStrictEqual(actions(lostDuringRecovery,'complete_task').map((call)=>call.payload.status),['exhausted']);

  for(const [surface,expectedAction] of [['challenge','pause_platform'],['login','platform_auth_result'],['error','complete_task']]){
    const failed=await runScenario([missingPage(),surface==='error'?{...missingPage(),page_type:'error'}:{...missingPage(),[surface==='challenge'?'challenged':'login_required']:true}],surface);
    assert.strictEqual(failed.events.filter((event)=>event.type==='search_scope_recovery').length,1);
    assert.strictEqual(actions(failed,expectedAction).length,1,`${surface} recovery surface must fail closed`);
    assert.strictEqual(actions(failed,'record_result').length,0);
    if(surface==='error')assert.deepStrictEqual(actions(failed,'complete_task').map((call)=>call.payload.status),['incomplete']);
  }

  console.log('Service-worker LinkedIn scope recovery regressions passed: bounded recovery, context handoff, fail-closed surfaces, exactly-once persistence');
})().catch((error)=>{console.error(error);process.exitCode=1;});
