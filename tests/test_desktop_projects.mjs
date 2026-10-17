import test from 'node:test';
import assert from 'node:assert/strict';
import { basename, join } from 'node:path';
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { ensureDesktopProjects } from '../plugin/scripts/setup-desktop.mjs';

const workspace = join(tmpdir(), 'fleet desktop test');
const tasks = join(workspace, 'custom tasks');
const workerConfig = { engine: 'codex', codex_app_folders: { main: 'AgentFleet', other: 'AgentFleet-其他' }, tasks_dir: tasks };
function fakeApp({ pending = false, installed = true } = {}) {
  const state = { 'local-projects': { main: { id: 'main', name: 'My fleet', rootPaths: [workspace] } } };
  const launches = [], directories = [], requests = [];
  const runtime = {
    platform: 'darwin', installed: () => installed, readState: () => state,
    requestRepair: request => requests.push(request),
    mkdir: path => directories.push(path), sleep: async () => {},
    launch: url => {
      launches.push(url);
      if (!pending) {
        const path = new URL(url).searchParams.get('path');
        state['local-projects'].other = { id: 'other', name: basename(path), rootPaths: [path] };
      }
    },
  };
  return { state, launches, directories, requests, runtime };
}

test('requests the configured exact name without opening a tasks project', async () => {
  const app = fakeApp();
  const report = await ensureDesktopProjects({ workspace, workerConfig }, app.runtime);
  assert.equal(report.status, 'pending');
  assert.equal(report.projects.main.project_name, 'My fleet');
  assert.equal(report.projects.other.workspace, tasks);
  assert.equal(report.projects.other.expected_name, 'AgentFleet-其他');
  assert.equal(report.projects.other.reason, 'waiting-for-desktop-close');
  assert.deepEqual(app.directories, []);
  assert.equal(app.launches.length, 0);
  assert.deepEqual(app.requests, [{ version: 1, other: {
    root: tasks, name: 'AgentFleet-其他', project_id: null, previous_name: null,
  } }]);
  // Only a later read of the completed offline registration can report success.
  app.state['local-projects'].other = { id: 'other', name: 'AgentFleet-其他', rootPaths: [tasks] };
  assert.equal((await ensureDesktopProjects({ workspace, workerConfig }, app.runtime)).status, 'registered');
});

test('repeat repair reuses both projects and preserves user names', async () => {
  const app = fakeApp();
  app.state['local-projects'].other = { id: 'other', name: 'Renamed other', rootPaths: [tasks] };
  const report = await ensureDesktopProjects({ workspace, workerConfig }, app.runtime);
  assert.equal(report.projects.other.project_name, 'Renamed other');
  assert.equal(app.launches.length, 0);
  assert.equal(app.requests.length, 0);
  assert.equal(Object.keys(app.state['local-projects']).length, 2);
});

test('pending second project prevents reporting overall success', async () => {
  const app = fakeApp({ pending: true });
  const report = await ensureDesktopProjects({ workspace, workerConfig }, app.runtime);
  assert.equal(report.projects.main.status, 'registered');
  assert.equal(report.projects.other.status, 'pending');
  assert.equal(report.status, 'pending');
  assert.match(report.message, /Quit Codex completely/);
});

test('repairs the observed default label without requesting arbitrary user renames', async () => {
  const app = fakeApp();
  app.state['local-projects'].other = { id: 'old', name: basename(tasks), rootPaths: [tasks] };
  const report = await ensureDesktopProjects({ workspace, workerConfig }, app.runtime);
  assert.equal(report.status, 'pending');
  assert.equal(app.requests[0].other.project_id, 'old');
  assert.equal(app.requests[0].other.previous_name, basename(tasks));
  assert.equal(app.launches.length, 0);
});

test('English requests use the English folder name', async () => {
  const app = fakeApp();
  const report = await ensureDesktopProjects({ workspace, workerConfig: { ...workerConfig, codex_app_folders: true, language: 'en' } }, app.runtime);
  assert.equal(report.projects.other.expected_name, 'AgentFleet-Other');
  assert.equal(app.requests[0].other.name, 'AgentFleet-Other');
});

test('missing desktop app is reported without pretending the second project was registered', async () => {
  const app = fakeApp({ installed: false });
  app.state['local-projects'] = {};
  const report = await ensureDesktopProjects({ workspace, workerConfig }, app.runtime);
  assert.equal(report.status, 'not-installed');
  assert.equal(app.launches.length, 0);
});

test('orchestrator, Claude and disabled folder sorting register only the main project', async () => {
  for (const config of [undefined, { ...workerConfig, executor: 'claude' }, { ...workerConfig, codex_app_folders: false },
    { ...workerConfig, codex_app_register_other_project: false }]) {
    const app = fakeApp();
    const report = await ensureDesktopProjects({ workspace, workerConfig: config }, app.runtime);
    assert.equal(report.project_name, 'My fleet');
    assert.equal(report.projects, undefined);
    assert.equal(app.launches.length, 0);
  }
});

test('no-desktop never reads app state, creates folders, or launches the app', async () => {
  const forbidden = () => { throw new Error('Unexpected desktop interaction'); };
  const report = await ensureDesktopProjects({ workspace, workerConfig, disabled: true }, {
    readState: forbidden, mkdir: forbidden, launch: forbidden,
  });
  assert.deepEqual(report, { status: 'disabled' });
});

test('invalid task paths are rejected before creating or registering any project', async () => {
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
    assert.equal(readFileSync(configPath, 'utf8'), privateConfig);
    const invalid = spawnSync(process.execPath, [...args, '--full-access'], { encoding: 'utf8' });
    assert.equal(invalid.status, 2);
    assert.match(invalid.stderr, /accepts only/);
    assert.equal(readFileSync(configPath, 'utf8'), privateConfig);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
