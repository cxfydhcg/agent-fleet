import { existsSync, mkdirSync, readFileSync, writeFileSync, renameSync } from 'node:fs';
import { homedir } from 'node:os';
import { resolve, join, normalize, win32, isAbsolute } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const projectForWorkspace = (state, workspace, platform = process.platform) => {
  const key = value => platform === 'win32' ? win32.normalize(value).toLowerCase() : normalize(value);
  return Object.values(state['local-projects'] || {}).find(project =>
    (project.rootPaths || []).some(root => typeof root === 'string' && key(root) === key(workspace)));
};

export const desktopWorkspaceUrl = workspace => 'codex://new?path=' + encodeURIComponent(workspace);

const stateReader = runtime => runtime.readState || (() => {
  const path = join(runtime.codexHome || process.env.CODEX_HOME || join(homedir(), '.codex'), '.codex-global-state.json');
  return existsSync(path) ? JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/, '')) : {};
});

function requestNamedProject(workerConfig, request, runtime) {
  if (runtime.requestRepair) return runtime.requestRepair(request);
  if (typeof workerConfig.state_dir !== 'string' || !isAbsolute(workerConfig.state_dir))
    throw new Error('Worker state_dir must be absolute. / 工作端状态目录必须是绝对路径。');
  // This is a worker request, never an edit to the running desktop's state.
  mkdirSync(workerConfig.state_dir, { recursive: true });
  const path = join(workerConfig.state_dir, 'desktop-project-repair.json');
  const temporary = `${path}.${process.pid}.tmp`;
  writeFileSync(temporary, JSON.stringify(request) + '\n', { mode: 0o600 });
  renameSync(temporary, path);
}

// Registration belongs to the running app. The dispatcher's offline thread
// sorter must not be responsible for making the second project visible.
export async function ensureDesktopProjects({ workspace, workerConfig, disabled = false }, runtime = {}) {
  const folders = workerConfig?.codex_app_folders;
  const otherEnabled = !!folders && workerConfig.codex_app_register_other_project !== false &&
    workerConfig.executor !== 'claude' && workerConfig.engine !== 'claude' && !disabled;
  if (otherEnabled && (typeof workerConfig.tasks_dir !== 'string' || !isAbsolute(workerConfig.tasks_dir)))
    throw new Error('Worker tasks_dir must be absolute. / 工作端任务目录必须是绝对路径。');
  const main = await ensureDesktopProject({ workspace, disabled }, runtime);
  if (!otherEnabled || !['registered', 'pending'].includes(main.status)) return main;
  const expectedName = typeof folders === 'object' && folders.other ||
    (workerConfig.language === 'en' ? 'AgentFleet-Other' : 'AgentFleet-其他');
  const existing = projectForWorkspace(stateReader(runtime)(), workerConfig.tasks_dir, runtime.platform || process.platform);
  const defaultName = workerConfig.tasks_dir.replace(/\\/g, '/').split('/').filter(Boolean).at(-1);
  let other;
  if (existing && (existing.name === expectedName || existing.name !== defaultName)) {
    // Keep an intentional user rename, but repair the default directory label.
    other = { status: 'registered', project_id: existing.id, project_name: existing.name, workspace: workerConfig.tasks_dir };
  } else {
    requestNamedProject(workerConfig, { version: 1, other: {
      root: workerConfig.tasks_dir, name: expectedName, project_id: existing?.id ?? null,
      previous_name: existing?.name ?? null,
    } }, runtime);
    other = { status: 'pending', workspace: workerConfig.tasks_dir, expected_name: expectedName,
      reason: 'waiting-for-desktop-close',
      message: `Quit Codex completely; the worker will create or repair "${expectedName}" while it is closed. / 请完全退出 Codex，工作端会在退出后创建或修正“${expectedName}”。` };
  }
  const projects = { main, other };
  // Keep the main project's existing top-level fields for older callers, but
  // never report successful registration if either project is still pending.
  const incomplete = Object.values(projects).find(project => project.status !== 'registered');
  return { ...main, status: incomplete?.status || 'registered', projects,
    ...(incomplete?.message ? { message: incomplete.message } : {}) };
}

export async function ensureDesktopProject({ workspace, disabled = false }, runtime = {}) {
  if (disabled) return { status: 'disabled' };
  const platform = runtime.platform || process.platform;
  if (platform !== 'win32' && platform !== 'darwin') return { status: 'not-available', reason: 'Automatic desktop registration supports Windows and macOS.' };
  workspace = resolve(workspace);
  const readState = stateReader(runtime);
  const known = projectForWorkspace(readState(), workspace, platform);
  const report = project => ({ status: 'registered', project_id: project.id, project_name: project.name, workspace });
  if (known) return report(known);
  const installed = runtime.installed || (() => {
    // macOS: whether any app handles codex:// links.
    if (platform === 'darwin') return spawnSync('osascript', ['-l', 'JavaScript', '-e',
      'ObjC.import("AppKit"); !!$.NSWorkspace.sharedWorkspace.URLForApplicationToOpenURL($.NSURL.URLWithString("codex://new"))'],
      { encoding: 'utf8', timeout: 10000 }).stdout.trim() === 'true';
    const result = spawnSync('powershell', ['-NoProfile', '-Command',
      "if (Test-Path -LiteralPath 'Registry::HKEY_CLASSES_ROOT\\codex') { exit 0 } else { exit 3 }"],
      { windowsHide: true, encoding: 'utf8', timeout: 10000 });
    if (result.error) throw result.error;
    if (result.status !== 0 && result.status !== 3) throw new Error('Could not check Codex desktop availability.');
    return result.status === 0;
  });
  if (!installed()) return { status: 'not-installed', workspace };
  const createDirectory = runtime.mkdir || (path => mkdirSync(path, { recursive: true }));
  createDirectory(workspace);
  const launch = runtime.launch || (url => {
    if (platform === 'darwin') {
      const opened = spawnSync('open', ['-g', url], { encoding: 'utf8', timeout: 15000 });
      if (opened.error || opened.status !== 0) throw new Error('Could not open the Agent Fleet project in Codex desktop.');
      return;
    }
    // The app owns project registration. Never rewrite its global-state file or
    // open the dispatcher's live main-agent thread, which would take its writer lock.
    const result = spawnSync('powershell', ['-NoProfile', '-Command',
      '$ErrorActionPreference="Stop"; Start-Process -FilePath $env:AGENT_FLEET_DESKTOP_URL -WindowStyle Hidden'],
      { windowsHide: true, encoding: 'utf8', timeout: 15000,
        env: { ...process.env, AGENT_FLEET_DESKTOP_URL: url } });
    if (result.error || result.status !== 0) throw new Error('Could not open the Agent Fleet project in Codex desktop.');
  });
  launch(desktopWorkspaceUrl(workspace));
  const sleep = runtime.sleep || (ms => new Promise(r => setTimeout(r, ms)));
  for (let attempt = 0; attempt < 20; attempt++) {
    await sleep(500);
    const project = projectForWorkspace(readState(), workspace, platform);
    if (project) return report(project);
  }
  return { status: 'pending', workspace,
    message: 'Codex has not confirmed project registration. Check for a folder-access prompt in the app, then rerun setup.' };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  let workspace, disabled = false;
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--no-desktop') disabled = true;
    else if (args[i] === '--workspace' && args[i+1] && !args[i+1].startsWith('--')) workspace = args[++i];
    else throw new Error('Usage: node setup-desktop.mjs --workspace PATH [--no-desktop]');
  }
  if (!workspace && !disabled) throw new Error('--workspace is required');
  try { console.log(JSON.stringify(await ensureDesktopProject({ workspace, disabled }), null, 2)); }
  catch { console.error('Desktop project setup failed. Worker data and sessions are preserved.'); process.exitCode = 1; }
}
