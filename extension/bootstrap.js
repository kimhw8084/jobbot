'use strict';
const q=new URLSearchParams(location.search);
const message={type:'JOBBOT_BOOTSTRAP_START',run_id:Number(q.get('run_id')||0),maintenance:q.get('maintenance')==='1',expected_build:String(q.get('expected_build')||''),refresh_id:String(q.get('refresh_id')||''),bridge_port:Number(q.get('bridge_port')||0),bridge_token:String(q.get('bridge_token')||'')};
chrome.runtime.sendMessage(message,()=>{try{window.close();}catch(_){} });
