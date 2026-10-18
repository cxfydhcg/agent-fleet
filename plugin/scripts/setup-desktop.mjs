import { existsSync, mkdirSync, readFileSync } from 'node:fs';
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
export const claudeWorkspaceUrl = workspace => 'claude://code/new?folder=' + encodeURIComponent(workspace);

const stateReader = runtime => runtime.readState || (() => {
  const path = join(runtime.codexHome || process.env.CODEX_HOME || join(homedir(), '.codex'), '.codex-global-state.json');
  return existsSync(path) ? JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/, '')) : {};
});

// Both projects are registered through the app's public workspace entry point.
export async function ensureDesktopProjects({ workspace, workerConfig, disabled = false }, runtime = {}) {
  const folders = workerConfig?.codex_app_folders;
  const otherEnabled = !!folders && workerConfig.codex_app_register_other_project !== false &&
    workerConfig.engine !== 'claude' && !disabled;
  if (otherEnabled) {
    if (typeof workerConfig.tasks_dir !== 'string' || !isAbsolute(workerConfig.tasks_dir))
      throw new Error('Worker tasks_dir must be absolute. / 工作端任务目录必须是绝对路径。');
    const expectedName = typeof folders === 'object' && folders.other ||
      (workerConfig.language === 'en' ? 'AgentFleet-Other' : 'AgentFleet-其他');
    const directoryName = workerConfig.tasks_dir.replace(/\\/g, '/').split('/').filter(Boolean).at(-1);
    if (directoryName !== expectedName)
      throw new Error('Task directory name must match the configured project name. / 任务目录名称必须与配置的项目名称一致。');
  }
  const main = await ensureDesktopProject({ workspace, disabled, engine: workerConfig?.engine }, runtime);
  const projects = { main };
  if (otherEnabled && ['registered', 'pending'].includes(main.status))
    projects.other = await ensureDesktopProject({ workspace: workerConfig.tasks_dir }, runtime);
  const incomplete = Object.values(projects).find(project => project.status !== 'registered');
  return { status: incomplete?.status || 'registered', projects,
    ...(incomplete?.message ? { message: incomplete.message } : {}) };
}

// Whether an app handles links with this scheme (Windows: a registered URL protocol).
const appInstalled = (platform, scheme, probeUrl) => () => {
  if (platform === 'darwin') return spawnSync('osascript', ['-l', 'JavaScript', '-e',
    `ObjC.import("AppKit"); !!$.NSWorkspace.sharedWorkspace.URLForApplicationToOpenURL($.NSURL.URLWithString(${JSON.stringify(probeUrl)}))`],
    { encoding: 'utf8', timeout: 10000 }).stdout.trim() === 'true';
  const result = spawnSync('powershell', ['-NoProfile', '-Command',
    `if (Test-Path -LiteralPath 'Registry::HKEY_CLASSES_ROOT\\${scheme}') { exit 0 } else { exit 3 }`],
    { windowsHide: true, encoding: 'utf8', timeout: 10000 });
  if (result.error) throw result.error;
  if (result.status !== 0 && result.status !== 3) throw new Error(`Could not check ${scheme} desktop availability.`);
  return result.status === 0;
};

const appLauncher = (platform, appName) => url => {
  if (platform === 'darwin') {
    const opened = spawnSync('open', ['-g', url], { encoding: 'utf8', timeout: 15000 });
    if (opened.error || opened.status !== 0) throw new Error(`Could not open the Agent Fleet project in ${appName} desktop.`);
    return;
  }
  // The app owns project registration. Never rewrite its global-state file or
  // open the dispatcher's live main-agent thread, which would take its writer lock.
  const result = spawnSync('powershell', ['-NoProfile', '-Command',
    '$ErrorActionPreference="Stop"; Start-Process -FilePath $env:AGENT_FLEET_DESKTOP_URL -WindowStyle Hidden'],
    { windowsHide: true, encoding: 'utf8', timeout: 15000,
      env: { ...process.env, AGENT_FLEET_DESKTOP_URL: url } });
  if (result.error || result.status !== 0) throw new Error(`Could not open the Agent Fleet project in ${appName} desktop.`);
};

export async function ensureDesktopProject({ workspace, disabled = false, engine = 'codex' }, runtime = {}) {
  if (disabled) return { status: 'disabled' };
  const platform = runtime.platform || process.platform;
  if (platform !== 'win32' && platform !== 'darwin') return { status: 'not-available', reason: 'Automatic desktop registration supports Windows and macOS.' };
  workspace = resolve(workspace);
  if (engine === 'claude') return openClaudeFolder(workspace, platform, runtime);
  const readState = stateReader(runtime);
  const known = projectForWorkspace(readState(), workspace, platform);
  const report = project => ({ status: 'registered', project_id: project.id, project_name: project.name, workspace });
  if (known) return report(known);
  const installed = runtime.installed || appInstalled(platform, 'codex', 'codex://new');
  if (!installed()) return { status: 'not-installed', workspace };
  const createDirectory = runtime.mkdir || (path => mkdirSync(path, { recursive: true }));
  createDirectory(workspace);
  const launch = runtime.launch || appLauncher(platform, 'Codex');
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

// A Claude worker's folder belongs in the Claude desktop Code tab, not in Codex. Claude desktop has
// no public project list to read back, so the result is 'opened', never 'registered': the app asks
// the owner to confirm the folder and then starts a Code session there.
async function openClaudeFolder(workspace, platform, runtime) {
  const installed = runtime.installed || appInstalled(platform, 'claude', 'claude://code/new');
  if (!installed()) return { status: 'not-installed', app: 'claude', workspace };
  const createDirectory = runtime.mkdir || (path => mkdirSync(path, { recursive: true }));
  createDirectory(workspace);
  const launch = runtime.launch || appLauncher(platform, 'Claude');
  launch(claudeWorkspaceUrl(workspace));
  return { status: 'opened', app: 'claude', workspace,
    message: 'Claude desktop opened a Code session in this folder. Confirm the folder prompt in the app.' };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  let workspace, disabled = false, engine = 'codex';
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--no-desktop') disabled = true;
    else if (args[i] === '--workspace' && args[i+1] && !args[i+1].startsWith('--')) workspace = args[++i];
    else if (args[i] === '--engine' && ['codex', 'claude'].includes(args[i+1])) engine = args[++i];
    else throw new Error('Usage: node setup-desktop.mjs --workspace PATH [--engine codex|claude] [--no-desktop]');
  }
  if (!workspace && !disabled) throw new Error('--workspace is required');
  try { console.log(JSON.stringify(await ensureDesktopProject({ workspace, disabled, engine }), null, 2)); }
  catch { console.error('Desktop project setup failed. Worker data and sessions are preserved.'); process.exitCode = 1; }
}
