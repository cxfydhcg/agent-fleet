import test from 'node:test';
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { ensureDesktopProject, projectForWorkspace } from '../plugin/scripts/setup-desktop.mjs';

test('reuse an existing project without opening a session or changing files', async () => {
  const workspace = resolve('fixture-existing');
  const state = {'local-projects':{one:{id:'one',name:'Existing custom label',rootPaths:[workspace]}}};
  const result = await ensureDesktopProject({workspace}, {platform:'win32',readState:()=>state,
    installed:()=>{throw Error('should not probe');},launch:()=>{throw Error('should not launch');},mkdir:()=>{throw Error('should not write');}});
  assert.equal(result.status,'registered');
  assert.equal(result.project_id,'one');
  assert.equal(result.project_name,'Existing custom label');
});

test('create through the desktop workspace URL and verify actual registration', async () => {
  const workspace = resolve("fixture 工作 & ' folder");
  let state = {'local-projects':{unrelated:{id:'other',rootPaths:[resolve('other')]}}};
  let created;
  const result = await ensureDesktopProject({workspace}, {platform:'win32',readState:()=>state,installed:()=>true,
    mkdir:path=>{created=path;},sleep:async()=>{},launch:url=>{
      const parsed = new URL(url);
      assert.equal(parsed.hostname,'new');
      assert.equal(parsed.searchParams.get('path'),workspace);
      assert.equal(parsed.searchParams.has('prompt'),false);
      state['local-projects'].fleet={id:'fleet',name:'AgentFleet',rootPaths:[workspace]};
    }});
  assert.equal(created,workspace);
  assert.equal(result.status,'registered');
  assert.equal(result.project_id,'fleet');
  assert.equal(Object.keys(state['local-projects']).length,2);
});

test('an unconfirmed launch stays pending instead of claiming success', async () => {
  const result = await ensureDesktopProject({workspace:resolve('fixture-pending')},
    {platform:'win32',readState:()=>({}),installed:()=>true,mkdir:()=>{},launch:()=>{},sleep:async()=>{}});
  assert.equal(result.status,'pending');
  assert.equal(result.project_id,undefined);
});

test('headless setup never reads app state or launches a desktop app', async () => {
  const result = await ensureDesktopProject({disabled:true},{readState:()=>{throw Error('unexpected read');}});
  assert.deepEqual(result,{status:'disabled'});
});

test('missing desktop app does not create a workspace or invoke its protocol', async () => {
  const result = await ensureDesktopProject({workspace:resolve('fixture-absent')},
    {platform:'win32',readState:()=>({}),installed:()=>false,mkdir:()=>{throw Error('unexpected write');},launch:()=>{throw Error('unexpected launch');}});
  assert.equal(result.status,'not-installed');
});

test('Windows root lookup ignores casing and distinguishes different folders', () => {
  const state={'local-projects':{fleet:{id:'fleet',rootPaths:['C:\\Users\\Me\\Documents\\AgentFleet']}}};
  assert.equal(projectForWorkspace(state,'c:\\users\\me\\documents\\agentfleet','win32').id,'fleet');
  assert.equal(projectForWorkspace(state,'C:\\Other\\AgentFleet','win32'),undefined);
});
