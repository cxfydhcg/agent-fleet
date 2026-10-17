import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const CLIENTS = ['plugin/skills/call-agent/fleet.mjs', 'plugin/scripts/fleet.mjs'].map(p => resolve(p));
const SETUP = resolve('plugin/scripts/setup-orchestrator.mjs');
const PAIRING = resolve('plugin/runtime/worker/tools/pairing.py');

function execute(executable, args, { cwd, env }) {
  const child = spawn(executable, args, { cwd, env });
  let stdout = '', stderr = '';
  child.stdout.on('data', d => { stdout += d; });
  child.stderr.on('data', d => { stderr += d; });
  return once(child, 'close').then(([code]) => ({ code, stdout, stderr }));
}

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'fleet-pair-role-'));
  const home = join(root, 'registry'), workspace = join(root, 'management workspace');
  mkdirSync(workspace);
  const env = { ...process.env, AGENT_FLEET_HOME: home, AGENT_FLEET_LANG: 'en' };
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const options = { cwd: workspace, env };
  return { root, home, workspace, options,
    client: (args, extra = {}, client = CLIENTS[0]) => execute(process.execPath, [client, ...args], { ...options, ...extra }),
    setup: () => execute(process.execPath, [SETUP, '--workspace', workspace, '--no-desktop'], options) };
}

async function waitingWorker(t) {
  const requests = [];
  const server = createServer(async (req, res) => {
    requests.push(req.url);
    let body = '';
    for await (const part of req) body += part;
    const send = value => { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(value)); };
    const address = `http://127.0.0.1:${server.address().port}/mcp`;
    if (req.url === '/pair/info') return send({ service: 'agent-fleet', pairing: true,
      computer_name: 'remote-pc', main_agent_id: 'remote-main', expires_in: 600 });
    if (req.url === '/pair/start' && req.headers.authorization === 'Bearer fixture-local-token') {
      return send({ code: '1234-5678', expires_in: 600, local_url: address, lan_url: address });
    }
    if (req.url === '/pair/claim' && JSON.parse(body).code === '1234-5678') {
      return send({ name: 'remote-pc', token: 'fixture-remote-token', main_agent_id: 'remote-main',
        main_agent_name: 'Remote Main', lan_url: address, local_url: address });
    }
    res.writeHead(403); res.end('{}');
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => new Promise(done => { server.close(done); server.closeAllConnections(); }));
  return { requests, address: `127.0.0.1:${server.address().port}`, port: server.address().port };
}

test('fresh clients reject discover and outgoing pair before network or filesystem changes', async t => {
  const f = fixture(t), worker = await waitingWorker(t);
  for (const client of CLIENTS) {
    for (const args of [['discover', `--hosts=${worker.address}`], ['pair', worker.address, '1234-5678']]) {
      const result = await f.client(args, {}, client);
      assert.equal(result.code, 2, result.stderr);
      assert.match(result.stderr, /setup as orchestrator or both/);
      assert.equal(existsSync(f.home), false);
    }
  }
  assert.deepEqual(worker.requests, []);
});

test('a pure worker registry cannot discover, pair, or import a remote connection', async t => {
  const f = fixture(t), worker = await waitingWorker(t);
  mkdirSync(f.home);
  const saved = JSON.stringify({ computers: { self: { main_agent_id: 'local-main', agents: {} } } });
  writeFileSync(join(f.home, 'computers.json'), saved);
  const connection = join(f.root, 'connection.json');
  writeFileSync(connection, JSON.stringify({ name: 'remote-pc', token: 'fixture-remote-token',
    lan_url: `http://${worker.address}/mcp`, local_url: `http://${worker.address}/mcp` }));
  for (const args of [['discover', `--hosts=${worker.address}`], ['pair', worker.address, '1234-5678'], ['add-computer', connection]]) {
    const result = await f.client(args);
    assert.equal(result.code, 2, result.stderr);
    assert.match(result.stderr, /setup as orchestrator or both/);
    assert.equal(readFileSync(join(f.home, 'computers.json'), 'utf8'), saved);
    assert.equal(existsSync(join(f.home, 'tokens')), false);
  }
  assert.deepEqual(worker.requests, []);
});

test('completed orchestrator setup allows discovery and pairing without printing credentials', async t => {
  const f = fixture(t), worker = await waitingWorker(t);
  const setup = await f.setup(); assert.equal(setup.code, 0, setup.stderr);
  const discovery = await f.client(['discover', `--hosts=${worker.address}`]);
  assert.equal(discovery.code, 0, discovery.stderr);
  assert.equal(JSON.parse(discovery.stdout)[0].computer_name, 'remote-pc');
  const result = await f.client(['pair', worker.address, '1234-5678']);
  assert.equal(result.code, 0, result.stderr);
  assert.equal(JSON.parse(result.stdout).main_agent_id, 'remote-main');
  assert.doesNotMatch(result.stdout + result.stderr, /fixture-remote-token/);
  assert.equal(readFileSync(join(f.home, 'tokens', 'remote-pc.txt'), 'utf8').trim(), 'fixture-remote-token');
});

test('other sessions cannot use a configured management registry to pair', async t => {
  const f = fixture(t), worker = await waitingWorker(t);
  assert.equal((await f.setup()).code, 0);
  const other = f.workspace + '-other'; mkdirSync(other);
  const before = readFileSync(join(f.home, 'computers.json'), 'utf8');
  for (const args of [['discover', `--hosts=${worker.address}`], ['pair', worker.address, '1234-5678']]) {
    const result = await f.client(args, { cwd: other });
    assert.equal(result.code, 2);
    assert.match(result.stderr, /configured orchestrator session/);
  }
  assert.deepEqual(worker.requests, []);
  assert.equal(readFileSync(join(f.home, 'computers.json'), 'utf8'), before);
});

test('worker self-registration remains available and cannot import a non-loopback local URL', async t => {
  const f = fixture(t);
  const connection = join(f.root, 'local-connection.json');
  const info = { name: 'self', token: 'fixture-local-token', main_agent_id: 'local-main',
    local_url: 'http://127.0.0.1:8765/mcp' };
  writeFileSync(connection, JSON.stringify(info));
  let result = await f.client(['add-computer', connection, '--local=1']);
  assert.equal(result.code, 0, result.stderr);
  const before = readFileSync(join(f.home, 'computers.json'), 'utf8');
  assert.equal(JSON.parse(before).orchestrator_workspace, undefined);
  writeFileSync(connection, JSON.stringify({ ...info, local_url: 'http://192.0.2.10:8765/mcp' }));
  result = await f.client(['add-computer', connection, '--local=1']);
  assert.equal(result.code, 2);
  assert.match(result.stderr, /loopback/);
  assert.equal(readFileSync(join(f.home, 'computers.json'), 'utf8'), before);
});

test('both setup capabilities retain the local worker when another worker is paired', async t => {
  const f = fixture(t), worker = await waitingWorker(t);
  assert.equal((await f.setup()).code, 0);
  const connection = join(f.root, 'local-connection.json');
  writeFileSync(connection, JSON.stringify({ name: 'self', token: 'fixture-local-token',
    main_agent_id: 'local-main', local_url: 'http://127.0.0.1:8765/mcp' }));
  assert.equal((await f.client(['add-computer', connection, '--local=1'])).code, 0);
  const result = await f.client(['pair', worker.address, '1234-5678']);
  assert.equal(result.code, 0, result.stderr);
  assert.deepEqual(Object.keys(JSON.parse(readFileSync(join(f.home, 'computers.json'), 'utf8')).computers).sort(), ['remote-pc', 'self']);
});

test('a worker can still generate its own pairing code without management setup', async t => {
  const f = fixture(t), worker = await waitingWorker(t);
  const token = join(f.root, 'worker-token.txt'); writeFileSync(token, 'fixture-local-token');
  const config = join(f.root, 'worker-config.json');
  writeFileSync(config, JSON.stringify({ computer_name: 'self', language: 'en', token_file: token, port: worker.port }));
  const python = process.env.PYTHON_FOR_TESTS || (process.platform === 'win32' ? 'python' : 'python3');
  const result = await execute(python, [PAIRING, '--config', config, '--json'], f.options);
  assert.equal(result.code, 0, result.stderr);
  assert.equal(JSON.parse(result.stdout).code, '1234-5678');
  assert.doesNotMatch(result.stdout + result.stderr, /fixture-local-token/);
  assert.deepEqual(worker.requests, ['/pair/start']);
  assert.equal(existsSync(f.home), false);
});
