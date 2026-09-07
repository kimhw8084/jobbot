'use strict';
const EXPECTED_EXTENSION_VERSION='3.2.0';
const state=document.getElementById('state'),btn=document.getElementById('start'),stopBtn=document.getElementById('stop'),emergencyBtn=document.getElementById('emergency'),qs=new URLSearchParams(location.search),runId=Number(qs.get('run_id')||0);
const bridgePort=Number(qs.get('bridge_port')||0),bridgeToken=String(qs.get('bridge_token')||'');
let bridgeReady=false;
function render(x){if(!x?.ok){state.textContent=`Bridge: ERROR\n${x?.error||'Unknown error'}${x?.message?'\n'+x.message:''}`;state.className='status bad';return;}if(x.run){const r=x.run,lines=[`Bridge: CONNECTED`,`Run #${r.browser_run_id} — ${r.status}`,`Mode: ${r.mode} | Platforms: ${r.platform}`,`Jobs: ${r.jobs_recorded} | new ${r.jobs_new} | updated ${r.jobs_updated} | unchanged ${r.jobs_unchanged}`,`Current task: ${r.current_task_id||'none'} | last progress: ${r.last_progress_at||'never'}`,`Last error: ${r.last_error||'none'}`,''];for(const p of x.platforms||[])lines.push(`${p.platform.padEnd(10)} auth=${p.auth_status.padEnd(17)} exhausted=${p.tasks_completed}/${p.tasks_total} incomplete=${p.tasks_incomplete} challenged=${p.tasks_challenged} failed=${p.tasks_failed} jobs=${p.jobs_recorded}`);const counts={};for(const t of x.tasks||[]){const k=`${t.platform}/${t.status}`;counts[k]=(counts[k]||0)+1;}lines.push('');for(const [k,n] of Object.entries(counts).sort())lines.push(`${k.padEnd(30)} ${n}`);const active=(x.tasks||[]).find(t=>t.status==='running');if(active){lines.push('',`Active: ${active.platform} · ${active.query_text}`,`Page ${active.page_number||active.pages_visited||0} · results ${active.results_seen||0} · details ${active.detail_count_read||0} · unique ${active.unique_jobs_recorded||0} · duplicates ${active.duplicate_sightings||0}`,`URL: ${active.current_search_url||'starting'}`,`Error: ${active.last_error||'none'}`);}state.textContent=lines.join('\n');state.className='status '+(r.status==='completed'?'ok':'');}else state.textContent=JSON.stringify(x,null,2);}
const send=(msg)=>new Promise((resolve)=>chrome.runtime.sendMessage(msg,x=>resolve(x||{ok:false,error:chrome.runtime.lastError?.message||'No response'})));
async function reloadStaleExtension(){
  const loaded=String(chrome.runtime.getManifest().version||'');
  if(loaded===EXPECTED_EXTENSION_VERSION)return false;
  if(!bridgePort||!bridgeToken||!runId)throw new Error(`Extension ${loaded||'unknown'} is stale; reload extension/ from chrome://extensions.`);
  state.textContent=`Updating unpacked JobBot extension ${loaded||'unknown'} → ${EXPECTED_EXTENSION_VERSION}…`;
  await chrome.storage.local.set({jobbot_bridge_config:{port:bridgePort,token:bridgeToken},jobbot_active_run_id:runId});
  setTimeout(()=>chrome.runtime.reload(),100);
  return true;
}
async function configure(){if(bridgeReady)return {ok:true};if(!bridgePort||!bridgeToken)return {ok:false,error:'Missing local bridge configuration. Launch this page using python -m jobbot run/resume or a platform launcher.'};const x=await send({type:'JOBBOT_CONFIGURE_BRIDGE',port:bridgePort,token:bridgeToken});bridgeReady=!!x?.ok;if(!bridgeReady)render(x);return x;}
async function status(){if(!runId)return render({ok:false,error:'Missing run_id'});if(!(await configure()).ok)return;render(await send({type:'JOBBOT_GET_STATUS',run_id:runId}));}
async function start(){btn.disabled=true;state.textContent='Connecting local bridge and starting / resuming normal-Chrome search…';try{if(await reloadStaleExtension())return;const c=await configure();if(!c.ok){btn.disabled=false;return;}render(await send({type:'JOBBOT_START_RUN',run_id:runId}));btn.disabled=false;status();}catch(e){render({ok:false,error:String(e?.message||e)});btn.disabled=false;}}
btn.addEventListener('click',start);setInterval(status,3000);status();if(qs.get('autorun')==='1')setTimeout(start,700);
async function stopAfter(){render(await send({type:'JOBBOT_STOP_AFTER_CURRENT',run_id:runId}));}
async function emergency(){if(!confirm('Stop the current read-only search immediately?'))return;render(await send({type:'JOBBOT_EMERGENCY_STOP',run_id:runId}));}
stopBtn?.addEventListener('click',stopAfter);emergencyBtn?.addEventListener('click',emergency);
