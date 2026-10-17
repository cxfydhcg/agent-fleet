import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer, request } from 'node:http';
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { once } from 'node:events';

const FLEET = resolve('plugin/skills/call-agent/fleet.mjs');
const TOKEN = 'worker-secret-token';

// A worker that only answers its API with the right token, like worker/server.py.
async function fakeWorker() {
  const seen = [];
  const server = createServer((req, res) => {
    seen.push({ url: req.url, auth: req.headers.authorization });
    if (req.url === '/dashboard') {
      res.writeHead(200, { 'Content-Type': 'text/html' });
      return res.end("<html><head><title>w</title></head><body><script>fetch('/api/tasks')</script></body></html>");
    }
    if (req.headers.authorization !== `Bearer ${TOKEN}`) { res.writeHead(401); return res.end('{}'); }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    if (req.url === '/api/profile') return res.end(JSON.stringify({ busy: { running: [{ task_id: 't1' }], queued: 2 }, agents: [{ name: 'main' }] }));
    if (req.url.startsWith('/api/tasks')) return res.end(JSON.stringify({ tasks: [{ task_id: 't1' }] }));
    res.end('{}');
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return { server, seen, url: `http://127.0.0.1:${server.address().port}/mcp` };
}

async function startBoard(home) {
  const child = spawn(process.execPath, [FLEET, 'dashboard', '--no-open', '--port=0'], { env: { ...process.env, AGENT_FLEET_HOME: home, AGENT_FLEET_LANG: 'en' } });
  let out = '';
  child.stdout.on('data', (d) => { out += d; });
  for (let i = 0; i < 100 && !/\?k=/.test(out); i++) await new Promise((r) => setTimeout(r, 50));
  const link = out.trim().split(/\s+/).find((s) => s.includes('?k='));
  assert.ok(link, 'board printed its link');
  return { child, link, base: new URL(link).origin };
}

test('the fleet board shows every worker without exposing tokens', async (t) => {
  const worker = await fakeWorker();
  const home = mkdtempSync(join(tmpdir(), 'fleet-board-'));
  mkdirSync(join(home, 'tokens'));
  writeFileSync(join(home, 'tokens', 'pc-1.txt'), TOKEN + '\n');
  writeFileSync(join(home, 'computers.json'), JSON.stringify({ computers: {
    'pc-1': { url: worker.url, token_file: join(home, 'tokens', 'pc-1.txt'), manager_name: 'boss', agents: {} },
    'pc-off': { url: 'http://127.0.0.1:1/mcp', token_file: join(home, 'tokens', 'pc-1.txt'), agents: {} },
  } }));
  const board = await startBoard(home);
  t.after(() => { board.child.kill(); worker.server.close(); rmSync(home, { recursive: true, force: true }); });

  assert.equal((await fetch(board.base + '/fleet/status')).status, 403, 'no key, no access');
  assert.equal((await fetch(board.base + '/?k=wrong', { redirect: 'manual' })).status, 403);
  const login = await fetch(board.link, { redirect: 'manual' });
  assert.equal(login.status, 302);
  const cookie = login.headers.get('set-cookie').split(';')[0];
  assert.match(login.headers.get('set-cookie'), /HttpOnly/);
  const get = (path, headers = {}) => fetch(board.base + path, { headers: { cookie, ...headers } });

  const rows = await (await get('/fleet/status')).json();
  const pc1 = rows.find((r) => r.name === 'pc-1'), off = rows.find((r) => r.name === 'pc-off');
  assert.equal(pc1.online, true);
  assert.equal(pc1.label, 'boss');
  assert.equal(pc1.busy.queued, 2);
  assert.equal(off.online, false);

  const page = await (await get('/')).text();
  assert.ok(!page.includes(TOKEN), 'the board page never contains a token');
  const tasks = await get('/w/pc-1/api/tasks?limit=5');
  assert.equal(tasks.status, 200);
  assert.deepEqual((await tasks.json()).tasks, [{ task_id: 't1' }]);
  assert.equal(worker.seen.at(-1).auth, `Bearer ${TOKEN}`, 'the board adds the token itself');
  assert.equal(worker.seen.at(-1).url, '/api/tasks?limit=5');

  const dash = await (await get('/w/pc-1/dashboard')).text();
  assert.match(dash, /<head><script>/, 'the worker page is routed through the board');
  assert.ok(dash.includes('"/w/pc-1"') && !dash.includes(TOKEN));

  assert.equal((await get('/w/pc-1/api/other')).status, 404, 'only the read-only API is forwarded');
  assert.equal((await get('/w/nobody/api/tasks')).status, 404);
  // fetch() ignores a Host header, so send this one by hand.
  const { port } = new URL(board.base);
  const rebinding = await new Promise((done, fail) => request({ host: '127.0.0.1', port, path: '/fleet/status',
    headers: { host: 'evil.example', cookie } }, (res) => { res.resume(); done(res.statusCode); }).on('error', fail).end());
  assert.equal(rebinding, 403, 'DNS rebinding is refused');
});
