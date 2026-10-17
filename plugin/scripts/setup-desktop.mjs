import { existsSync, mkdirSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { resolve, join, normalize, win32 } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const projectForWorkspace = (state, workspace, platform = process.platform) => {
  const key = value => platform === 'win32' ? win32.normalize(value).toLowerCase() : normalize(value);
  return Object.values(state['local-projects'] || {}).find(project =>
    (project.rootPaths || []).some(root => typeof root === 'string' && key(root) === key(workspace)));
};

export const desktopWorkspaceUrl = workspace => 'codex://new?path=' + encodeURIComponent(workspace);

export async function ensureDesktopProject({ workspace, disabled = false }, runtime = {}) {
  if (disabled) return { status: 'disabled' };
  const platform = runtime.platform || process.platform;
  if (platform !== 'win32' && platform !== 'darwin') return { status: 'not-available', reason: 'Automatic desktop registration supports Windows and macOS.' };
  workspace = resolve(workspace);
  const statePath = join(runtime.codexHome || process.env.CODEX_HOME || join(homedir(), '.codex'), '.codex-global-state.json');
  const readState = runtime.readState || (() => {
    if (!existsSync(statePath)) return {};
    return JSON.parse(readFileSync(statePath, 'utf8').replace(/^\uFEFF/, ''));
  });
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
