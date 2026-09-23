'use strict';

const assert=require('assert');
const fs=require('fs');
const vm=require('vm');

const WORKER=fs.readFileSync('extension/service_worker.js','utf8');
const LINKEDIN_URL='https://www.linkedin.com/jobs/search/?keywords=patient&location=United%20States&start=12';
const INDEED_URL='https://www.indeed.com/jobs?q=data+quality&l=United+States';
const RECEIVER_ERROR='Could not establish connection. Receiving end does not exist.';

async function run(){
  const calls=[],events=[],listeners=[],tabs=new Map(),windows=new Map();
  const state={linkedinReloads:0,linkedinReadyProbeStarted:false,linkedinDone:false,indeedDone:false};
  let releaseLinkedInReady;
  const linkedinReadyGate=new Promise(resolve=>{releaseLinkedInReady=resolve;});
  const linkedinTab={id:11,windowId:7,status:'complete',url:LINKEDIN_URL,active:false};
  const indeedTab={id:12,windowId:8,status:'complete',url:INDEED_URL,active:false};
  tabs.set(11,linkedinTab);tabs.set(12,indeedTab);
  windows.set(7,{id:7,state:'minimized',focused:false,type:'normal',tabs:[linkedinTab]});
  windows.set(8,{id:8,state:'minimized',focused:false,type:'normal',tabs:[indeedTab]});
  const emitLinkedInAttachment=()=>new Promise(resolve=>{
    const u=new URL(linkedinTab.url),generation='linkedin-reloaded-document';
    listeners[0]({type:'JOBBOT_CONTENT_SCRIPT_ATTACHED',phase:'platform_receiver_ready',platform:'linkedin',document_url:`${u.origin}${u.pathname}`,document_origin:u.origin,document_path:u.pathname,query_keys:[...u.searchParams.keys()],attachment_generation:generation,document_generation:generation,ready_state:'interactive'},{tab:{id:linkedinTab.id,windowId:linkedinTab.windowId},frameId:0,documentId:generation,url:linkedinTab.url},resolve);
  });
  const chrome={
    runtime:{getManifest:()=>({version_name:'3.2.2-prod-ready.672cf88.26'}),getURL:path=>`chrome-extension://jobbot/${path}`,onMessage:{addListener:listener=>listeners.push(listener)},onStartup:{addListener:()=>{}},onInstalled:{addListener:()=>{}},reload:()=>{}},
    storage:{local:{get:async()=>({jobbot_bridge_config:{port:43123,token:'x'.repeat(24)}}),set:async()=>{},remove:async()=>{}}},
    windows:{get:async id=>windows.get(id)||(()=>{throw new Error(`window ${id} missing`);})(),remove:async id=>windows.delete(id),create:async()=>{throw new Error('recovery must not create a target for a readable human gate');}},
    alarms:{create:()=>{},onAlarm:{addListener:()=>{}}},
    tabs:{
      get:async id=>tabs.get(id)||(()=>{throw new Error(`tab ${id} missing`);})(),
      update:async(id,details)=>Object.assign(await chrome.tabs.get(id),details),
      reload:async id=>{if(id!==linkedinTab.id)throw new Error('unexpected platform reload');state.linkedinReloads+=1;linkedinTab.status='complete';},
      move:async()=>{},create:async()=>{throw new Error('standalone detail tabs are forbidden');},remove:async id=>tabs.delete(id),
      sendMessage:async(id,message)=>{
        const tab=await chrome.tabs.get(id);
        if(id===linkedinTab.id&&message.type==='JOBBOT_RECEIVER_READY'){
          state.linkedinReadyProbeStarted=true;
          await linkedinReadyGate;
          await emitLinkedInAttachment();
          return{receiver_ready:true,receiver_attached:true,platform_receiver_ready:true,inspection_ready:true,dom_ready:true,attachment_generation:'linkedin-reloaded-document',document_generation:'linkedin-reloaded-document',platform:'linkedin',page_url:tab.url,surface:'challenge',challenged:true,challenge_reason:'fixture human gate'};
        }
        if(id===linkedinTab.id&&state.linkedinReloads===0)throw new Error(RECEIVER_ERROR);
        if(id===linkedinTab.id)return{platform:'linkedin',page_type:'search',page_url:tab.url,ready:true,authenticated:true,auth_state:'verified',result_links:[],exhausted:true,extraction_scope_missing:false};
        if(id===indeedTab.id)return{platform:'indeed',page_type:'search',page_url:tab.url,ready:true,authenticated:true,auth_state:'verified',result_links:[],exhausted:true,extraction_scope_missing:false};
        throw new Error(`unexpected target ${id}`);
      },
    },
  };
  const rpc=async(action,payload)=>{
    calls.push({action,payload});
    if(action==='browser_event')events.push(payload);
    if(action==='should_stop')return{ok:true,stop:false};
    if(action==='task_progress')return{ok:true};
    if(action==='complete_task'){
      if(payload.task_id===1661)state.linkedinDone=true;
      if(payload.task_id===1662)state.indeedDone=true;
      return{ok:true};
    }
    if(action==='next_pending_detail')return{ok:true,done:true,pending_count:0};
    return{ok:true};
  };
  const sandbox={chrome,console,URL,URLSearchParams,AbortController,Date,Error,JSON,Map,Set,Promise,String,Number,Math,Array,Object,RegExp,TypeError,setTimeout:(fn)=>{fn();return 0;},clearTimeout:()=>{},fetch:async(_url,options)=>{const body=JSON.parse(options.body);return{ok:true,status:200,json:async()=>rpc(body.action,body)};}};
  vm.runInNewContext(`${WORKER}\nglobalThis.__processTask=processTask;`,sandbox,{filename:'extension/service_worker.js'});

  let linkedinOutcome=null;
  const linkedinTask={task_id:1661,platform:'linkedin',requested_search_url:LINKEDIN_URL,search_url:LINKEDIN_URL,receiver_ready_timeout_ms:250,checkpoint_json:JSON.stringify({search_url:LINKEDIN_URL,page_number:6,processed:2,page_fingerprint:'L0'})};
  const linkedinPromise=sandbox.__processTask(166,linkedinTask,{tab:linkedinTab,window_id:7,owned_window:true},'worker-166-linkedin').then(value=>{linkedinOutcome=value;});
  while(!state.linkedinReadyProbeStarted)await new Promise(resolve=>setTimeout(resolve,0));
  assert.strictEqual(state.linkedinDone,false,'LinkedIn remains in its bounded receiver wait');

  const indeedTask={task_id:1662,platform:'indeed',requested_search_url:INDEED_URL,search_url:INDEED_URL,checkpoint_json:'{}'};
  await sandbox.__processTask(166,indeedTask,{tab:indeedTab,window_id:8,owned_window:true},'worker-166-indeed');
  assert.strictEqual(state.indeedDone,true,'the healthy Indeed worker completes during LinkedIn receiver recovery');
  assert.strictEqual(state.linkedinDone,false,'Indeed progress does not wait for or resume LinkedIn');

  releaseLinkedInReady();
  await linkedinPromise;
  assert.strictEqual(state.linkedinReloads,1);
  assert.strictEqual(linkedinOutcome?.blocked,true,'the observed LinkedIn challenge remains a human gate');
  assert.strictEqual(calls.filter(call=>call.action==='pause_platform'&&call.payload.platform==='linkedin').length,1);
  assert.strictEqual(calls.filter(call=>call.action==='pause_platform'&&call.payload.platform==='indeed').length,0);
  assert.strictEqual(events.filter(event=>event.event_type==='detail_navigation').length,0);
  assert.strictEqual(tabs.size,2,'receiver recovery did not create an extra tab');
  assert.strictEqual(windows.size,2,'one platform gate did not create an extra window');
  assert.strictEqual(windows.get(7).focused,false);assert.strictEqual(windows.get(8).focused,false);
  assert.strictEqual(linkedinTab.active,false);assert.strictEqual(indeedTab.active,false);
  console.log('CHG-166 worker isolation regression passed: Indeed completes while LinkedIn is waiting; LinkedIn challenge remains platform-local with no focus or target leakage');
}

run().catch(error=>{console.error(error);process.exitCode=1;});
