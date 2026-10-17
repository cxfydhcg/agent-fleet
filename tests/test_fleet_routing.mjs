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
async function fakeDispatcher() {
  const started = [], tasks = [], prompts = [];
  const server = createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    const { id, method, params } = JSON.parse(body);
    const reply = (result) => { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ jsonrpc: '2.0', id, result })); };
    if (method !== 'tools/call') return reply({});
    const args = params.arguments, tool = params.name;
    const result = (value) => reply({ structuredContent: { result: value } });
    if (tool === 'get_profile') return result({ main_agent: { id: 'main-1', name: '主agent export' }, agents: [] });
    if (tool === 'list_tasks') return result({ tasks });
    if (tool === 'submit_task' || tool === 'continue_task') {
      prompts.push(args.prompt);
      const task = { task_id: `t${tasks.length + 1}`, name: args.name ?? tasks.find((t) => t.task_id === args.task_id)?.name,
                     thread_id: `th-${args.name ?? args.task_id}`, status: 'completed', result: 'ok' };
      tasks.unshift(task);
      started.push({ tool, name: task.name, ...(args.task_id ? { continued: args.task_id } : {}) });
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

test('only the orchestrator folder reaches the main agent; other sessions get their own agent', async (t) => {
  const worker = await fakeDispatcher();
  const root = mkdtempSync(join(tmpdir(), 'fleet-routing-'));
  const home = join(root, 'registry'), orchestrator = join(root, 'AgentFleet'), project = join(root, 'my-project');
  for (const dir of [join(home, 'tokens'), orchestrator, project]) mkdirSync(dir, { recursive: true });
  writeFileSync(join(home, 'tokens', 'pc-1.txt'), 'token\n');
  writeFileSync(join(home, 'computers.json'), JSON.stringify({ orchestrator_workspace: orchestrator,
    computers: { 'pc-1': { url: worker.url, token_file: join(home, 'tokens', 'pc-1.txt'), agents: {} } } }));
  t.after(() => { worker.server.close(); rmSync(root, { recursive: true, force: true }); });
  const base = { ...process.env, AGENT_FLEET_HOME: home, AGENT_FLEET_LANG: 'en' };
  delete base.CLAUDE_CODE_SESSION_ID; delete base.CODEX_THREAD_ID;

  let run = await fleet(['export', 'pc-1', 'hello'], { cwd: orchestrator, env: base });
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
  run = await fleet(['check', 'pc-1', 'do something else'], { cwd: project, env: session });
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
