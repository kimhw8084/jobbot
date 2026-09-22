'use strict';

const assert=require('assert');
const fs=require('fs');
const vm=require('vm');

const BOOTSTRAP=fs.readFileSync('extension/receiver_bootstrap.js','utf8');
const listeners=[];
const sent=[];
const chrome={runtime:{onMessage:{addListener:listener=>listeners.push(listener)},sendMessage:message=>{sent.push(message);return Promise.resolve({ok:true});}}};
const sandbox={chrome,URL,Promise,Date,Math,globalThis:{},location:{hostname:'www.linkedin.com',href:'https://www.linkedin.com/jobs/search/?keywords=private%20term&access_token=secret&start=150',origin:'https://www.linkedin.com',pathname:'/jobs/search/'},document:{readyState:'loading'}};
vm.runInNewContext(BOOTSTRAP,sandbox,{filename:'extension/receiver_bootstrap.js'});
assert.strictEqual(listeners.length,1,'bootstrap must register its receiver before platform hydration');
assert.strictEqual(sent.length,1,'bootstrap must emit one bounded attachment signal');
assert.strictEqual(sent[0].type,'JOBBOT_CONTENT_SCRIPT_ATTACHED');
assert.strictEqual(sent[0].phase,'bootstrap');
assert.strictEqual(sent[0].platform,'linkedin');
assert(sent[0].attachment_generation&&sent[0].document_generation);
assert.strictEqual(sent[0].document_url,'https://www.linkedin.com/jobs/search/');
assert.deepStrictEqual(Array.from(sent[0].query_keys),['access_token','keywords','start']);
assert(!JSON.stringify(sent[0]).includes('private%20term'));
assert(!JSON.stringify(sent[0]).includes('secret'));

const respond=message=>{let value;const returned=listeners[0](message,{},response=>{value=response;});assert.strictEqual(returned,true);return value;};
const earlyReady=respond({type:'JOBBOT_RECEIVER_READY'});
assert.strictEqual(earlyReady.receiver_attached,true);
assert.strictEqual(earlyReady.bootstrap_only,true);
assert.strictEqual(earlyReady.platform_receiver_ready,false);
const deferred=respond({type:'JOBBOT_INSPECT_SEARCH_EVENTUALLY'});
assert.strictEqual(deferred.inspect_deferred,true);
assert.strictEqual(deferred.ready,false);
assert.strictEqual(deferred.extraction_scope_missing,true);

sandbox.globalThis.JobBotPlatformReceiverReady=true;
sandbox.document.readyState='interactive';
const readyAfterPlatformLoad=respond({type:'JOBBOT_RECEIVER_READY'});
assert.strictEqual(readyAfterPlatformLoad.receiver_attached,true);
assert.strictEqual(readyAfterPlatformLoad.bootstrap_only,false);
assert.strictEqual(readyAfterPlatformLoad.platform_receiver_ready,true);
assert.strictEqual(readyAfterPlatformLoad.inspection_ready,true);

console.log('Receiver bootstrap regressions passed: document_start attachment, token/query-value redaction, truthful DOM deferral, and post-hydration readiness');
