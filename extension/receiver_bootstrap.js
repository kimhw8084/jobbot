(() => {
  'use strict';

  const host=String(location.hostname||'').toLowerCase();
  const platform=host==='linkedin.com'||host.endsWith('.linkedin.com')?'linkedin':host==='indeed.com'||host.endsWith('.indeed.com')?'indeed':host==='glassdoor.com'||host.endsWith('.glassdoor.com')?'glassdoor':'';
  if(!platform)return;
  const generation=`${Date.now()}-${Math.random().toString(36).slice(2,10)}`;
  const safeDocumentUrl=()=>{try{const u=new URL(location.href);return`${u.origin}${u.pathname}`;}catch(_){return'';}};
  const queryKeys=()=>{try{return[...new URL(location.href).searchParams.keys()].sort().slice(0,32);}catch(_){return[];}};
  const base=()=>({platform,document_url:safeDocumentUrl(),document_origin:location.origin,document_path:location.pathname.slice(0,500),query_keys:queryKeys(),attachment_generation:generation,document_generation:generation,ready_state:document.readyState});
  const sendAttachment=(phase)=>{try{const message={type:'JOBBOT_CONTENT_SCRIPT_ATTACHED',phase,...base()};const result=chrome.runtime.sendMessage(message);if(result?.catch)result.catch(()=>{});}catch(_){} };
  const inspectTypes=new Set(['JOBBOT_INSPECT','JOBBOT_INSPECT_AUTH','JOBBOT_INSPECT_SEARCH','JOBBOT_INSPECT_SEARCH_EVENTUALLY','JOBBOT_INSPECT_SEARCH_PANE','JOBBOT_INSPECT_DETAIL','JOBBOT_SCROLL_AND_INSPECT','JOBBOT_ADVANCE_SEARCH']);

  // This listener is intentionally declarative and document_start. It is the
  // minimum receiver that survives a slow SPA hydration and reports attachment
  // without claiming that the DOM-dependent platform receiver is ready.
  chrome.runtime.onMessage.addListener((message,_sender,sendResponse)=>{
    if(message?.type==='JOBBOT_RECEIVER_READY'){
      sendResponse({...base(),receiver_ready:true,receiver_attached:true,platform_receiver_ready:globalThis.JobBotPlatformReceiverReady===true,bootstrap_only:globalThis.JobBotPlatformReceiverReady!==true,inspection_ready:globalThis.JobBotPlatformReceiverReady===true,dom_ready:document.readyState!=='loading',surface:'unknown',surface_reason:'receiver bootstrap attached; DOM surface is not assessed here'});
      return true;
    }
    if(!globalThis.JobBotPlatformReceiverReady&&inspectTypes.has(message?.type)){
      sendResponse({...base(),receiver_attached:true,receiver_ready:true,platform_receiver_ready:false,bootstrap_only:true,inspection_ready:false,dom_ready:false,inspect_deferred:true,ready:false,page_type:'loading',page_url:location.href,result_links:[],extraction_scope_missing:true,surface:'loading',surface_reason:'platform content script is still loading'});
      return true;
    }
    return false;
  });

  globalThis.JobBotReceiverAttachment={generation,signalReady:()=>sendAttachment('platform_receiver_ready')};
  sendAttachment('bootstrap');
})();
