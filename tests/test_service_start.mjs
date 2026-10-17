import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer } from 'node:http';
import { startWorker } from '../plugin/scripts/start.mjs';

function fixture(t, overrides = {}) {
  const root = mkdtempSync(join(tmpdir(), 'fleet-start-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const workspace = join(root, '工作目录 with spaces');
  const runtime = join(workspace, 'service', 'worker');
  mkdirSync(runtime, { recursive: true });
  const tokenFile = join(root, 'token.txt');
  writeFileSync(tokenFile, 'fixture-private-token\n');
  const config = { tasks_dir: join(workspace, 'tasks'), port: 8765, token_file: tokenFile,
    computer_name: 'fixture-pc', language: 'en', ...overrides };
  const configPath = join(root, '配置 with spaces.json');
  writeFileSync(configPath, '\uFEFF' + JSON.stringify(config));
  for (const name of ['start.sh', 'start.ps1']) writeFileSync(join(runtime, name), '');
  return { root, runtime, config, configPath };
}

test('launches the installed runtime and checks authenticated health', { skip: process.platform === 'win32' }, async t => {
  const server = createServer((req, res) => {
    if (req.url !== '/health' || req.headers.authorization !== 'Bearer fixture-private-token') {
      res.writeHead(401); res.end(); return;
    }
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ service: 'codex-task-mcp' }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }));
  const f = fixture(t, { port: server.address().port });
  const script = join(f.runtime, 'start.sh');
  writeFileSync(script, '#!/bin/bash\nprintf "%s\\n" "$1" "$2" > "$0.args"\n');
  const before = readFileSync(f.configPath, 'utf8');
  const answer = await startWorker({ configPath: f.configPath, platform: 'darwin' });
  assert.equal(answer.status, 'ready');
  assert.equal(answer.computer, 'fixture-pc');
  assert.equal(answer.dashboard, `http://127.0.0.1:${f.config.port}/dashboard`);
  assert.equal(readFileSync(script + '.args', 'utf8'), `--config\n${f.configPath}\n`);
  assert.equal(readFileSync(f.configPath, 'utf8'), before);
  assert.doesNotMatch(JSON.stringify(answer), /fixture-private-token/);
});

test('Windows uses argument arrays and the configured runtime', async t => {
  const f = fixture(t);
  let launch;
  const answer = await startWorker({ platform: 'win32', env: { AGENT_FLEET_CONFIG: f.configPath },
    run(executable, args, options) { launch = { executable, args, options }; return { status: 0 }; },
    request: async () => ({ ok: true, json: async () => ({ service: 'codex-task-mcp' }) }) });
  assert.equal(answer.status, 'ready');
  assert.equal(launch.executable, 'powershell.exe');
  assert.deepEqual(launch.args, ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
    join(f.runtime, 'start.ps1'), '-ConfigPath', f.configPath]);
  assert.equal(launch.options.cwd, f.runtime);
  assert.equal(launch.options.shell, undefined);
});

test('missing installation does not run setup', async t => {
  const f = fixture(t);
  let called = false;
  await assert.rejects(startWorker({ platform: 'darwin', configPath: join(f.root, 'missing.json'),
    run() { called = true; } }), /use Agent Fleet setup/);
  assert.equal(called, false);
});

test('launcher failure does not expose captured secrets', async t => {
  const f = fixture(t);
  let checked = false;
  await assert.rejects(startWorker({ platform: 'darwin', configPath: f.configPath,
    run: () => ({ status: 1, stderr: 'fixture-private-token', stdout: 'sensitive config' }),
    request() { checked = true; } }), error => {
    assert.match(error.message, /Startup failed/);
    assert.doesNotMatch(error.message, /fixture-private-token|sensitive config/);
    return true;
  });
  assert.equal(checked, false);
});

test('launcher success requires valid worker health', async t => {
  const f = fixture(t);
  for (const reply of [{ ok: false }, { ok: true, json: async () => ({ service: 'unrelated' }) }]) {
    await assert.rejects(startWorker({ platform: 'darwin', configPath: f.configPath,
      run: () => ({ status: 0 }), request: async () => reply }), /Service is not ready/);
  }
});

test('empty credential fails before launch', async t => {
  const f = fixture(t);
  writeFileSync(f.config.token_file, '\n');
  let called = false;
  await assert.rejects(startWorker({ platform: 'darwin', configPath: f.configPath,
    run() { called = true; } }), /credential is empty/);
  assert.equal(called, false);
});
