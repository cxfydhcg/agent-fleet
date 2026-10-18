import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, join, resolve } from 'node:path';
import { once } from 'node:events';

const FLEET = resolve('plugin/skills/call-agent/fleet.mjs');

// A dispatcher that finishes every task at once and records what was started.
async function fakeDispatcher(mains = [{ id: 'main-1', name: '主agent export' }], trackMains = false) {
  const started = [], tasks = [], prompts = [];
  const server = createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    const { id, method, params } = JSON.parse(body);
    const reply = (result) => { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ jsonrpc: '2.0', id, result })); };
    if (method !== 'tools/call') return reply({});
    const args = params.arguments, tool = params.name;
    const result = (value) => reply({ structuredContent: { result: value } });
    if (tool === 'get_profile') return result({ main_agent: mains[0], main_agents: mains, engines: ['codex', 'claude'], agents: [] });
    if (tool === 'list_tasks') return result({ tasks });
    if (tool === 'submit_task' || tool === 'continue_task') {
      prompts.push(args.prompt);
      const main = trackMains && mains.find(m => m.name === args.name || (args.task_id && m.task_id === args.task_id));
      const task = { task_id: `t${tasks.length + 1}`, name: main?.name ?? args.name ?? tasks.find((t) => t.task_id === args.task_id)?.name,
                     thread_id: `th-${args.name ?? args.task_id}`, status: 'completed', result: 'ok' };
      tasks.unshift(task);
      if (main) main.task_id = task.task_id;
      started.push({ tool, name: task.name, ...(args.workspace ? { workspace: args.workspace } : {}), ...(args.task_id ? { continued: args.task_id } : {}) });
      return result({ task_id: task.task_id });
    }
    if (tool === 'get_task') return result(tasks.find((t) => t.task_id === args.task_id));
    result({});
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return { server, started, prompts, url: `http://127.0.0.1:${server.address().port}/mcp` };
}

function fleet(args, { cwd, env }) {
  const child = spawn(process.execPath, [FLEET, ...args], { cwd, env });
  let stdout = '', stderr = '';
  child.stdout.on('data', (d) => { stdout += d; });
  child.stderr.on('data', (d) => { stderr += d; });
  return once(child, 'exit').then(([code]) => ({ code, stdout, stderr }));
}

test('second main agent uses its identity and workspace, is orchestrator-only, and check covers both', async t => {
  const root = mkdtempSync(join(tmpdir(), 'fleet-dual-'));
  const mains = [{ id: 'main-code', name: 'Code 经理', engine: 'codex', workspace: join(root, 'main-codex') },
                 { id: 'main-claude', name: 'Claude 经理', engine: 'claude', workspace: join(root, 'main-claude') }];
  const worker = await fakeDispatcher(mains, true);
  const home = join(root, 'registry'); mkdirSync(join(home, 'tokens'), { recursive: true });
  const tokenFile = join(home, 'tokens', 'fixture.txt'); writeFileSync(tokenFile, 'fixture-only');
  writeFileSync(join(home, 'computers.json'), JSON.stringify({ orchestrator_session: 'owner', computers: {
    dual: { main_agent_name: mains[0].name, main_agent_id: mains[0].id, main_agents: mains,
            engine: 'codex', engines: ['codex', 'claude'], url: worker.url, token_file: tokenFile, agents: {} } } }));
  const env = { ...process.env, AGENT_FLEET_HOME: home, AGENT_FLEET_LANG: 'en', CLAUDE_CODE_SESSION_ID: 'owner' }; delete env.CODEX_THREAD_ID;
  t.after(() => { worker.server.close(); worker.server.closeAllConnections(); rmSync(root, { recursive: true, force: true }); });
  const run = (...args) => fleet(args, { cwd: root, env });
  let result = await run('agent', 'dual', mains[1].name, 'hello');
  assert.equal(result.code, 0, result.stderr);
  assert.deepEqual(worker.started.at(-1), { tool: 'submit_task', name: mains[1].name, workspace: mains[1].workspace });
  result = await run('agent', 'dual', mains[1].id, 'continue');
  assert.equal(result.code, 0, result.stderr);
  assert.equal(worker.started.at(-1).name, mains[1].name);
  result = await fleet(['agent', 'dual', mains[1].name, 'from another session'], { cwd: root,
    env: { ...env, CLAUDE_CODE_SESSION_ID: 'other' } });
  assert.equal(result.code, 0, result.stderr);
  assert.notEqual(worker.started.at(-1).name, mains[1].name);
  const count = worker.started.length;
  result = await run('check', 'dual');
  assert.equal(result.code, 0, result.stderr);
  assert.deepEqual(worker.started.slice(count).map(s => s.name), mains.map(m => m.name));
  result = await run('check', 'dual', mains[1].name);
  assert.equal(result.code, 0, result.stderr);
  assert.equal(worker.started.at(-1).name, mains[1].name);
  const unknown = await run('check', 'dual', 'unknown');
  assert.notEqual(unknown.code, 0);
  assert.match(unknown.stderr, /Main agent was not found/);
  result = await run('export', 'dual', 'default');
  assert.equal(result.code, 0, result.stderr);
  assert.equal(worker.started.at(-1).name, mains[0].name);
  const oldName = mains[1].name;
  mains[1].name = 'Claude renamed';
  result = await fleet(['agent', 'dual', mains[1].name, 'new name from another session'], {
    cwd: root, env: { ...env, CLAUDE_CODE_SESSION_ID: 'other' } });
  assert.equal(result.code, 0, result.stderr);
  assert.notEqual(worker.started.at(-1).name, mains[1].name);
  result = await run('agent', 'dual', mains[1].name, 'renamed main');
  assert.equal(result.code, 0, result.stderr);
  assert.equal(worker.started.at(-1).tool, 'continue_task');
  assert.equal(worker.started.at(-1).name, mains[1].name);
  const pc = JSON.parse(readFileSync(join(home, 'computers.json'), 'utf8')).computers.dual;
  assert.equal(pc.main_agent_id, mains[0].id);
  assert.equal(pc.main_agents[1].id, mains[1].id);
  assert.equal(pc.agents[oldName], undefined);
});

test('only the registered orchestrator session reaches the main agent; other sessions get their own agent', async (t) => {
  const worker = await fakeDispatcher();
  const root = mkdtempSync(join(tmpdir(), 'fleet-routing-'));
  const home = join(root, 'registry'), project = join(root, 'my-project');
  for (const dir of [join(home, 'tokens'), project]) mkdirSync(dir, { recursive: true });
  writeFileSync(join(home, 'tokens', 'pc-1.txt'), 'token\n');
  writeFileSync(join(home, 'computers.json'), JSON.stringify({ orchestrator_session: 'orch-session-1',
    computers: { 'pc-1': { main_agent_name: '主agent export', main_agent_id: 'main-1', engine: 'codex', url: worker.url, token_file: join(home, 'tokens', 'pc-1.txt'), agents: {} } } }));
  t.after(() => { worker.server.close(); rmSync(root, { recursive: true, force: true }); });
  const base = { ...process.env, AGENT_FLEET_HOME: home, AGENT_FLEET_LANG: 'en' };
  delete base.CLAUDE_CODE_SESSION_ID; delete base.CODEX_THREAD_ID;

  // The orchestrator is a session, not a folder: it reaches the main agent from any directory.
  let run = await fleet(['export', 'pc-1', 'hello'], { cwd: project, env: { ...base, CLAUDE_CODE_SESSION_ID: 'orch-session-1' } });
  assert.equal(run.code, 0, run.stderr);
  assert.deepEqual(worker.started.at(-1), { tool: 'submit_task', name: '主agent export' });

  const own = `${basename(project)} · abcdef12`;
  const session = { ...base, CLAUDE_CODE_SESSION_ID: 'abcdef12-3456-7890' };
  run = await fleet(['export', 'pc-1', 'hello'], { cwd: project, env: session });
  assert.equal(run.code, 0, run.stderr);
  assert.deepEqual(worker.started.at(-1), { tool: 'submit_task', name: own });
  assert.match(run.stderr, /not the orchestrator session/);

  // The same session keeps its agent, also when it names the main agent explicitly.
  const first = worker.started.length;
  run = await fleet(['agent', 'pc-1', '主agent export', 'again'], { cwd: project, env: session });
  assert.equal(run.code, 0, run.stderr);
  assert.equal(worker.started.at(-1).tool, 'continue_task');
  assert.equal(worker.started.at(-1).name, own);
  assert.equal(worker.started.length, first + 1);
  assert.ok(!worker.started.slice(1).some((s) => s.name === '主agent export'), 'no other session reached the main agent');

  // The installation check reaches the main agent from any session, and carries only its fixed message.
  run = await fleet(['check', 'pc-1'], { cwd: project, env: session });
  assert.equal(run.code, 0, run.stderr);
  assert.deepEqual(worker.started.at(-1), { tool: 'submit_task', name: '主agent export' });
  assert.match(worker.prompts.at(-1), /^Installation check: reply with exactly OK/);
  assert.doesNotMatch(worker.prompts.at(-1), /do something else/);

  // Without a session id there is nothing to name the agent after.
  run = await fleet(['export', 'pc-1', 'hello'], { cwd: project, env: base });
  assert.equal(run.code, 2);
  assert.match(run.stderr, /--from=/);
  run = await fleet(['export', 'pc-1', 'hello', '--from=nightly'], { cwd: project, env: base });
  assert.equal(run.code, 0, run.stderr);
  assert.equal(worker.started.at(-1).name, `${basename(project)} · nightly`);
  assert.ok(JSON.parse(readFileSync(join(home, 'computers.json'), 'utf8')).computers['pc-1'].agents[own], 'registry remembers the agent');

  // A titled session is named after its task; a second session with the same title gets a number.
  const titled = { ...base, CLAUDE_CODE_SESSION_ID: 'titled-session-1' };
  run = await fleet(['export', 'pc-1', 'hello', '--title=系统设置截图'], { cwd: project, env: titled });
  assert.equal(run.code, 0, run.stderr);
  assert.deepEqual(worker.started.at(-1), { tool: 'submit_task', name: '系统设置截图' });
  run = await fleet(['export', 'pc-1', 'hello', '--title=系统设置截图'], { cwd: project, env: { ...base, CLAUDE_CODE_SESSION_ID: 'titled-session-2' } });
  assert.equal(run.code, 0, run.stderr);
  assert.deepEqual(worker.started.at(-1), { tool: 'submit_task', name: '系统设置截图 2' });
  // Later sends from the first session find its agent without a title, and a new title doesn't rename it.
  run = await fleet(['export', 'pc-1', 'more', '--title=别的名字'], { cwd: project, env: titled });
  assert.equal(run.code, 0, run.stderr);
  assert.equal(worker.started.at(-1).tool, 'continue_task');
  assert.equal(worker.started.at(-1).name, '系统设置截图');
});

test('an unreachable computer gets a clear message, not a stack trace', async (t) => {
  const closed = createServer();
  closed.listen(0, '127.0.0.1');
  await once(closed, 'listening');
  const port = closed.address().port;
  closed.close();
  const root = mkdtempSync(join(tmpdir(), 'fleet-down-'));
  const home = join(root, 'registry');
  mkdirSync(join(home, 'tokens'), { recursive: true });
  writeFileSync(join(home, 'tokens', 'pc-1.txt'), 'token\n');
  writeFileSync(join(home, 'computers.json'), JSON.stringify({ computers: { 'pc-1': { main_agent_name: 'main', engine: 'codex',
    url: `http://127.0.0.1:${port}/mcp`, token_file: join(home, 'tokens', 'pc-1.txt'), agents: {} } } }));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const result = await fleet(['get', 'pc-1', 'some-task'], { cwd: root, env: { ...process.env, AGENT_FLEET_HOME: home, AGENT_FLEET_LANG: 'en' } });
  assert.equal(result.code, 4);
  assert.match(result.stderr, /Cannot reach pc-1/);
  assert.doesNotMatch(result.stderr, /at |undici/);
});
