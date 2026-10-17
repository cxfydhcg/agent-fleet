import { spawnSync } from 'node:child_process';
import { cpSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ensureDesktopProject } from './setup-desktop.mjs';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const values = new Map();
const flags = new Set();
const booleans = new Set(['plan', 'local', 'full-access', 'restricted', 'skip-prerequisites', 'no-autostart', 'reconfigure', 'no-desktop']);
const valued = new Set(['role', 'engine', 'connectivity', 'workspace', 'data-dir', 'name', 'main-agent-name', 'manager-name', 'language', 'description',
  'codex-executable', 'connection', 'registry-home', 'claude-executable', 'port', 'tailscale-port', 'lan-address']);
const argv = process.argv.slice(2);

function fail(message) { console.error(message); process.exit(2); }
for (let i = 0; i < argv.length; i++) {
  const key = argv[i].startsWith('--') ? argv[i].slice(2) : '';
  if (booleans.has(key)) flags.add(key);
  else if (valued.has(key) && argv[i + 1] && !argv[i + 1].startsWith('--')) values.set(key, argv[++i]);
  else fail('Usage: node setup.mjs --role orchestrator|worker|both [--plan] [role options]');
}
const role = values.get('role');
if (!['orchestrator', 'worker', 'both'].includes(role)) fail('Choose a role: orchestrator, worker, or both.');
if (values.has('engine') && !['codex', 'claude'].includes(values.get('engine'))) fail('Engine must be codex or claude.');
if (values.has('connectivity') && !['local', 'tailscale', 'lan'].includes(values.get('connectivity'))) fail('Connectivity must be local, tailscale or lan.');
if (values.has('lan-address') && values.get('connectivity') !== 'lan') fail('--lan-address requires --connectivity lan.');
if (values.has('language') && !['zh', 'en'].includes(values.get('language'))) fail('Language must be zh or en.');
if (role === 'worker' && (values.has('connection') || values.has('registry-home') || flags.has('local'))) fail('Connection/registry options require orchestrator or both.');
const workerOptions = ['engine', 'connectivity', 'data-dir', 'name', 'main-agent-name', 'manager-name', 'description', 'claude-executable', 'codex-executable', 'port', 'tailscale-port', 'lan-address'];
if (role === 'orchestrator' && (workerOptions.some(k => values.has(k)) || ['full-access', 'restricted', 'skip-prerequisites', 'no-autostart', 'reconfigure'].some(k => flags.has(k)))) fail('Worker options require worker or both.');
// A worker's permissions are never left to a default: whoever runs setup must ask the owner and pass the choice.
if (flags.has('full-access') && flags.has('restricted')) fail('Pass either --full-access or --restricted, not both.');
if (role !== 'orchestrator' && !flags.has('plan') && !flags.has('full-access') && !flags.has('restricted'))
  fail('Ask the owner how much this worker may do, then pass --full-access (sub-tasks can operate the desktop and apps, ' +
       'including Computer Use; every permission request is approved) or --restricted (sub-tasks only write their working ' +
       'folder; Computer Use is refused). --plan shows the current mode.');
if (flags.has('local') && !values.has('connection')) fail('--local requires --connection.');
if (role !== 'orchestrator' && !['win32', 'darwin'].includes(process.platform)) fail('Worker setup currently supports Windows and macOS.');
for (const key of ['port', 'tailscale-port']) {
  if (values.has(key) && (!/^\d+$/.test(values.get(key)) || Number(values.get(key)) < 1 || Number(values.get(key)) > 65535)) fail('Ports must be integers from 1 to 65535.');
}
const registryHome = resolve(values.get('registry-home') || process.env.AGENT_FLEET_HOME || join(homedir(), '.agent-fleet'));
if (values.has('connection')) {
  let info;
  try { info = JSON.parse(readFileSync(resolve(values.get('connection')), 'utf8').replace(/^\uFEFF/, '')); }
  catch { fail('Could not read a valid connection file.'); }
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(info.name || '') || !info.token || !(flags.has('local') ? info.local_url : (info.tailscale_url || info.lan_url))) fail('Connection is invalid for the selected local/remote mode.');
}
function run(executable, args, env = process.env) {
  const child = spawnSync(executable, args, { env, encoding: 'utf8', windowsHide: true });
  if (child.stdout) process.stdout.write(child.stdout);
  if (child.stderr) process.stderr.write(child.stderr);
  if (child.error) throw child.error;
  if (child.status !== 0) process.exit(child.status || 1);
  return child.stdout || '';
}
function lastObject(output) {
  for (let offset = output.lastIndexOf('{'); offset >= 0; offset = output.lastIndexOf('{', offset - 1)) {
    try { return JSON.parse(output.slice(offset).trim()); } catch { /* earlier command output */ }
    if (offset === 0) break;
  }
  throw new Error('Worker did not report its installation settings.');
}

// macOS: deploy the runtime into the workspace (so autostart does not point into the plugin cache, which
// changes on updates), run the Mac installer, then register the worker for this computer's main-agent skill.
function setupMacWorker() {
  const dataDir = resolve(values.get('data-dir') || join(homedir(), 'Library', 'Application Support', 'agent-fleet'));
  const configPath = join(dataDir, 'config.json');
  const previous = existsSync(configPath) ? JSON.parse(readFileSync(configPath, 'utf8').replace(/^\uFEFF/, '')) : null;
  const workspace = resolve(values.get('workspace') || (previous?.tasks_dir ? dirname(previous.tasks_dir) : join(homedir(), 'Documents', 'AgentFleet')));
  const args = ['--data-dir', dataDir, '--workspace', workspace, '--json'];
  for (const key of ['engine', 'connectivity', 'name', 'main-agent-name', 'manager-name', 'language', 'description', 'claude-executable', 'codex-executable', 'port', 'tailscale-port', 'lan-address'])
    if (values.has(key)) args.push('--' + key, values.get(key));
  for (const key of ['full-access', 'restricted', 'skip-prerequisites', 'no-autostart', 'reconfigure', 'plan']) if (flags.has(key)) args.push('--' + key);
  if (flags.has('plan')) return lastObject(run('bash', [join(root, 'runtime', 'install-worker.sh'), ...args]));
  const deployment = join(workspace, 'service');
  mkdirSync(deployment, { recursive: true });
  cpSync(join(root, 'runtime', 'worker'), join(deployment, 'worker'), { recursive: true, force: true });
  for (const file of ['install-worker.sh', 'LICENSE']) cpSync(join(root, 'runtime', file), join(deployment, file), { force: true });
  const result = lastObject(run('bash', [join(deployment, 'install-worker.sh'), ...args]));
  const clientHome = join(dataDir, 'local-client');
  const env = { ...process.env, AGENT_FLEET_HOME: clientHome };
  const client = join(root, 'scripts', 'fleet.mjs');
  run(process.execPath, [client, 'add-computer', result.connection_file, '--local=1'], env);
  if (result.status === 'service-ready') {
    run(process.execPath, [client, 'describe-agent', result.computer, result.main_agent_name,
      result.language === 'en' ? '--role=Main agent responsible for this worker' : '--role=负责这台电脑的主 Agent', '--type=main'], env);
  }
  return { ...result, data_dir: dataDir, client_home: clientHome };
}

let worker;
if (role !== 'orchestrator' && process.platform === 'darwin') {
  worker = setupMacWorker();
} else if (role !== 'orchestrator') {
  const args = ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', join(root, 'scripts/setup-worker.ps1'), '-NoDesktop'];
  const mappings = { engine: 'Engine', connectivity: 'Connectivity', workspace: 'Workspace', 'data-dir': 'DataDir',
    name: 'Name', 'main-agent-name': 'MainAgentName', 'manager-name': 'ManagerName', language: 'Language', description: 'Description', 'claude-executable': 'ClaudeExecutable', port: 'Port', 'tailscale-port': 'TailscalePort', 'lan-address': 'LanAddress' };
  for (const [key, parameter] of Object.entries(mappings)) if (values.has(key)) args.push('-' + parameter, values.get(key));
  const switches = { plan: 'Plan', 'full-access': 'FullAccess', restricted: 'Restricted', 'skip-prerequisites': 'SkipPrerequisites', 'no-autostart': 'NoAutostart', reconfigure: 'Reconfigure' };
  for (const [key, parameter] of Object.entries(switches)) if (flags.has(key)) args.push('-' + parameter);
  worker = lastObject(run('powershell', args));
}
if (flags.has('plan')) {
  console.log(JSON.stringify({ role, plan: true, ...(worker ? { worker } : {}),
    desktop: { enabled: !flags.has('no-desktop'), workspace: values.get('workspace') || worker?.workspace || join(homedir(),'Documents','AgentFleet') },
    ...(role !== 'worker' ? { orchestrator: { registry_home: registryHome, local_worker_registration: role === 'both', connection_file: values.get('connection') || null } } : {}) }, null, 2));
  process.exit(0);
}
// The worker's own client (main-agent skill) treats sessions in the worker's workspace as the orchestrator session,
// so its main agent stays reachable from there instead of falling back to a default folder.
if (worker?.client_home && worker.workspace) {
  const local = join(worker.client_home, 'computers.json');
  if (existsSync(local)) {
    const saved = JSON.parse(readFileSync(local, 'utf8').replace(/^\uFEFF/, ''));
    if (saved.orchestrator_workspace !== resolve(worker.workspace)) {
      saved.orchestrator_workspace = resolve(worker.workspace);
      writeFileSync(local, JSON.stringify(saved, null, 2) + '\n');
    }
  }
}
if (role !== 'worker') {
  const env = { ...process.env, AGENT_FLEET_HOME: registryHome };
  const setup = join(root, 'scripts/setup-orchestrator.mjs');
  // The project folder the orchestrator session works in; only sessions there may talk to main agents.
  const place = ['--workspace', resolve(values.get('workspace') || worker?.workspace || join(homedir(), 'Documents', 'AgentFleet'))];
  run(process.execPath, [setup, ...place, '--no-desktop'], env);
  if (values.has('language')) run(process.execPath, [join(root, 'skills/call-agent/fleet.mjs'), 'language', values.get('language')], env);
  if (role === 'both') {
    if (!worker.connection_file || !existsSync(worker.connection_file)) throw new Error('Worker connection file is missing; no local worker was registered.');
    run(process.execPath, [setup, ...place, '--connection', worker.connection_file, '--local', '--no-desktop'], env);
  }
  if (values.has('connection')) run(process.execPath, [setup, ...place, '--connection', resolve(values.get('connection')), ...(flags.has('local') ? ['--local'] : []), '--no-desktop'], env);
}
const desktop = await ensureDesktopProject({workspace: values.get('workspace') || worker?.workspace || join(homedir(),'Documents','AgentFleet'), disabled:flags.has('no-desktop')});
if (worker) worker.desktop = desktop;
console.log(JSON.stringify({ role, status: desktop.status === 'pending' ? 'desktop-setup-pending' : 'setup-complete', desktop, ...(worker ? { worker } : {}),
  ...(role !== 'worker' ? { registry_home: registryHome } : {}) }, null, 2));
