import test from 'node:test';
import assert from 'node:assert/strict';
import { basename, join } from 'node:path';
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { ensureDesktopProjects } from '../plugin/scripts/setup-desktop.mjs';

const workspace = join(tmpdir(), 'fleet desktop test');
const tasks = join(workspace, 'AgentFleet-其他');
const workerConfig = { engine: 'codex', codex_app_folders: { main: 'AgentFleet', other: 'AgentFleet-其他' }, tasks_dir: tasks };
function fakeApp({ pending = false, installed = true } = {}) {
  const state = { 'local-projects': { main: { id: 'main', name: 'My fleet', rootPaths: [workspace] } } };
  const launches = [], directories = [];
  const runtime = {
    platform: 'darwin', installed: () => installed, readState: () => state,
    mkdir: path => directories.push(path), sleep: async () => {},
    launch: url => {
      launches.push(url);
      if (!pending) {
        const path = new URL(url).searchParams.get('path');
        state['local-projects'].other = { id: 'other', name: basename(path), rootPaths: [path] };
      }
    },
  };
  return { state, launches, directories, runtime };
}

test('localized real directories register immediately and repeat setup reuses the project', async () => {
  for (const [language, name] of [['zh', 'AgentFleet-其他'], ['en', 'AgentFleet-Other']]) {
    const app = fakeApp();
    const config = { ...workerConfig, language, codex_app_folders: true, tasks_dir: join(workspace, name) };
    const report = await ensureDesktopProjects({ workspace, workerConfig: config }, app.runtime);
    assert.equal(report.status, 'registered');
    assert.equal(report.projects.main.project_name, 'My fleet');
    assert.equal(report.projects.other.project_name, name);
    assert.equal(new URL(app.launches[0]).searchParams.get('path'), config.tasks_dir);
    assert.equal(report.project_id, undefined, 'only the projects object contains project fields');
    const again = await ensureDesktopProjects({ workspace, workerConfig: config }, app.runtime);
    assert.equal(again.projects.other.project_id, report.projects.other.project_id);
    assert.equal(app.launches.length, 1);
  }
});

test('registration remains pending until confirmed by the app', async () => {
  const app = fakeApp({ pending: true });
  const report = await ensureDesktopProjects({ workspace, workerConfig }, app.runtime);
  assert.equal(report.status, 'pending');
  assert.equal(report.projects.main.status, 'registered');
  assert.equal(report.projects.other.status, 'pending');
});

test('repeat setup preserves intentional project renames', async () => {
  const app = fakeApp();
  app.state['local-projects'].other = { id: 'other', name: 'Renamed other', rootPaths: [tasks] };
  const report = await ensureDesktopProjects({ workspace, workerConfig }, app.runtime);
  assert.equal(report.projects.other.project_name, 'Renamed other');
  assert.equal(app.launches.length, 0);
  assert.equal(Object.keys(app.state['local-projects']).length, 2);
});

test('unsupported directory names fail before app interaction instead of queuing a migration', async () => {
  const forbidden = () => { throw Error('Unexpected desktop interaction'); };
  await assert.rejects(ensureDesktopProjects({ workspace,
    workerConfig: { ...workerConfig, tasks_dir: join(workspace, 'tasks') } }, { readState: forbidden }), /must match/);
});

test('missing desktop app is reported without claiming registration', async () => {
  const app = fakeApp({ installed: false });
  app.state['local-projects'] = {};
  const report = await ensureDesktopProjects({ workspace, workerConfig }, app.runtime);
  assert.equal(report.status, 'not-installed');
  assert.equal(app.launches.length, 0);
});

test('orchestrator and disabled folder sorting register only the main project', async () => {
  for (const config of [undefined, { ...workerConfig, codex_app_folders: false },
    { ...workerConfig, codex_app_register_other_project: false }]) {
    const app = fakeApp();
    const report = await ensureDesktopProjects({ workspace, workerConfig: config }, app.runtime);
    assert.equal(report.projects.main.project_name, 'My fleet');
    assert.equal(report.projects.other, undefined);
    assert.equal(app.launches.length, 0);
  }
});

test('a Claude worker opens its folder in Claude desktop and never touches Codex', async () => {
  const app = fakeApp({ pending: true });
  app.runtime.readState = () => { throw new Error('Codex state must not be read for a Claude worker'); };
  const report = await ensureDesktopProjects({ workspace, workerConfig: { ...workerConfig, engine: 'claude' } }, app.runtime);
  assert.equal(report.status, 'opened');
  assert.equal(report.projects.main.app, 'claude');
  assert.equal(report.projects.other, undefined);
  assert.equal(app.launches.length, 1);
  const url = new URL(app.launches[0]);
  assert.equal(url.protocol, 'claude:');
  assert.equal(url.hostname, 'code');
  assert.equal(url.pathname, '/new');
  assert.equal(url.searchParams.get('folder'), workspace);
  assert.deepEqual(app.directories, [workspace]);
});

test('no-desktop never reads app state, creates folders, or launches the app', async () => {
  const forbidden = () => { throw new Error('Unexpected desktop interaction'); };
  const report = await ensureDesktopProjects({ workspace, workerConfig, disabled: true }, {
    readState: forbidden, mkdir: forbidden, launch: forbidden,
  });
  assert.deepEqual(report, { status: 'disabled', projects: { main: { status: 'disabled' } } });
});

test('invalid task paths fail before app interaction', async () => {
  const forbidden = () => { throw new Error('Unexpected desktop interaction'); };
  for (const tasks_dir of [undefined, 'relative tasks', 123]) {
    await assert.rejects(ensureDesktopProjects({ workspace, workerConfig: { ...workerConfig, tasks_dir } }, {
      readState: forbidden, mkdir: forbidden, launch: forbidden,
    }), /must be absolute/);
  }
});

test('desktop-only preview keeps private config unchanged and rejects worker-setting changes', () => {
  const dir = mkdtempSync(`${tmpdir()}/fleet-desktop-test-`);
  const configPath = `${dir}/config.json`;
  const privateConfig = JSON.stringify({ ...workerConfig, token: 'private-test-sentinel', sandbox: 'danger-full-access' });
  writeFileSync(configPath, privateConfig);
  try {
    const args = [fileURLToPath(new URL('../plugin/scripts/setup.mjs', import.meta.url)), '--role', 'worker', '--language', 'zh', '--data-dir', dir, '--desktop-only'];
    const plan = spawnSync(process.execPath, [...args, '--plan'], { encoding: 'utf8' });
    assert.equal(plan.status, 0, plan.stderr);
    assert.equal(JSON.parse(plan.stdout).other_workspace, tasks);
    assert.doesNotMatch(plan.stdout + plan.stderr, /private-test-sentinel/);
    const invalid = spawnSync(process.execPath, [...args, '--full-access'], { encoding: 'utf8' });
    assert.equal(invalid.status, 2);
    assert.match(invalid.stderr, /accepts only/);
    assert.equal(readFileSync(configPath, 'utf8'), privateConfig);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
