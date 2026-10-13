import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ensureDesktopProject } from './setup-desktop.mjs';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const values = new Map();
const flags = new Set();
const booleans = new Set(['plan', 'local', 'full-access', 'skip-prerequisites', 'no-autostart', 'reconfigure', 'no-desktop']);
const valued = new Set(['role', 'engine', 'connectivity', 'workspace', 'data-dir', 'name', 'main-agent-name', 'description',
  'connection', 'registry-home', 'claude-executable', 'port', 'tailscale-port']);
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
if (values.has('connectivity') && !['local', 'tailscale'].includes(values.get('connectivity'))) fail('Connectivity must be local or tailscale.');
if (role === 'worker' && (values.has('connection') || values.has('registry-home') || flags.has('local'))) fail('Connection/registry options require orchestrator or both.');
const workerOptions = ['engine', 'connectivity', 'data-dir', 'name', 'main-agent-name', 'description', 'claude-executable', 'port', 'tailscale-port'];
if (role === 'orchestrator' && (workerOptions.some(k => values.has(k)) || ['full-access', 'skip-prerequisites', 'no-autostart', 'reconfigure'].some(k => flags.has(k)))) fail('Worker options require worker or both.');
if (flags.has('local') && !values.has('connection')) fail('--local requires --connection.');
if (role !== 'orchestrator' && process.platform !== 'win32') fail('Worker setup currently requires Windows.');
for (const key of ['port', 'tailscale-port']) {
  if (values.has(key) && (!/^\d+$/.test(values.get(key)) || Number(values.get(key)) < 1 || Number(values.get(key)) > 65535)) fail('Ports must be integers from 1 to 65535.');
}
const registryHome = resolve(values.get('registry-home') || process.env.AGENT_FLEET_HOME || join(homedir(), '.agent-fleet'));
if (values.has('connection')) {
  let info;
  try { info = JSON.parse(readFileSync(resolve(values.get('connection')), 'utf8').replace(/^\uFEFF/, '')); }
  catch { fail('Could not read a valid connection file.'); }
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(info.name || '') || !info.token || !(flags.has('local') ? info.local_url : info.tailscale_url)) fail('Connection is invalid for the selected local/remote mode.');
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

let worker;
if (role !== 'orchestrator') {
  const args = ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', join(root, 'scripts/setup-worker.ps1'), '-NoDesktop'];
  const mappings = { engine: 'Engine', connectivity: 'Connectivity', workspace: 'Workspace', 'data-dir': 'DataDir',
    name: 'Name', 'main-agent-name': 'MainAgentName', description: 'Description', 'claude-executable': 'ClaudeExecutable', port: 'Port', 'tailscale-port': 'TailscalePort' };
  for (const [key, parameter] of Object.entries(mappings)) if (values.has(key)) args.push('-' + parameter, values.get(key));
  const switches = { plan: 'Plan', 'full-access': 'FullAccess', 'skip-prerequisites': 'SkipPrerequisites', 'no-autostart': 'NoAutostart', reconfigure: 'Reconfigure' };
  for (const [key, parameter] of Object.entries(switches)) if (flags.has(key)) args.push('-' + parameter);
  worker = lastObject(run('powershell', args));
}
if (flags.has('plan')) {
  console.log(JSON.stringify({ role, plan: true, ...(worker ? { worker } : {}),
    desktop: { enabled: !flags.has('no-desktop'), workspace: values.get('workspace') || worker?.workspace || join(homedir(),'Documents','AgentFleet') },
    ...(role !== 'worker' ? { orchestrator: { registry_home: registryHome, local_worker_registration: role === 'both', connection_file: values.get('connection') || null } } : {}) }, null, 2));
  process.exit(0);
}
if (role !== 'worker') {
  const env = { ...process.env, AGENT_FLEET_HOME: registryHome };
  const setup = join(root, 'scripts/setup-orchestrator.mjs');
  run(process.execPath, [setup, '--no-desktop'], env);
  if (role === 'both') {
    if (!worker.connection_file || !existsSync(worker.connection_file)) throw new Error('Worker connection file is missing; no local worker was registered.');
    run(process.execPath, [setup, '--connection', worker.connection_file, '--local', '--no-desktop'], env);
  }
  if (values.has('connection')) run(process.execPath, [setup, '--connection', resolve(values.get('connection')), ...(flags.has('local') ? ['--local'] : []), '--no-desktop'], env);
}
const desktop = await ensureDesktopProject({workspace: values.get('workspace') || worker?.workspace || join(homedir(),'Documents','AgentFleet'), disabled:flags.has('no-desktop')});
if (worker) worker.desktop = desktop;
console.log(JSON.stringify({ role, status: desktop.status === 'pending' ? 'desktop-setup-pending' : 'setup-complete', desktop, ...(worker ? { worker } : {}),
  ...(role !== 'worker' ? { registry_home: registryHome } : {}) }, null, 2));
