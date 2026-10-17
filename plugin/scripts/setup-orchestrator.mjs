import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { ensureDesktopProject } from './setup-desktop.mjs';

const args = process.argv.slice(2);
let connection, local = false, home = process.env.AGENT_FLEET_HOME || join(homedir(), '.agent-fleet');
let workspace = join(homedir(),'Documents','AgentFleet'), noDesktop = false;
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--local') local = true;
  else if (args[i] === '--no-desktop') noDesktop = true;
  else if (['--connection', '--home','--workspace'].includes(args[i]) && args[i + 1] && !args[i + 1].startsWith('--')) {
    const key = args[i++];
    if (key === '--connection') connection = resolve(args[i]); else if(key === '--workspace') workspace = resolve(args[i]); else home = resolve(args[i]);
  } else {
    console.error('Usage: node setup-orchestrator.mjs [--home PATH] [--connection PATH] [--local] [--workspace PATH] [--no-desktop]');
    process.exit(2);
  }
}
if (local && !connection) { console.error('--local requires --connection'); process.exit(2); }
if (Number(process.versions.node.split('.')[0]) < 18) { console.error('Node.js 18 or newer is required'); process.exit(2); }
if (connection) {
  let info;
  try { info = JSON.parse(readFileSync(connection, 'utf8').replace(/^\uFEFF/, '')); }
  catch { console.error('Could not read a valid connection file.'); process.exit(2); }
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(info.name || '')) {
    console.error('Computer name must be an ASCII identifier, without path separators.'); process.exit(2);
  }
  if (!info.name || !info.token || !(local ? info.local_url : (info.tailscale_url || info.lan_url))) {
    console.error(local ? 'Connection needs name, token and local_url.' : 'Remote connection needs a Tailscale or LAN URL; set up that worker for remote access first.');
    process.exit(2);
  }
  if ((typeof info.main_agent_name !== 'string' || !info.main_agent_name.trim() || info.main_agent_name.trim().length > 100 || /[\u0000-\u001f\u007f]/.test(info.main_agent_name) || info.main_agent_name.trim().startsWith('--') || ['__proto__','constructor','prototype'].includes(info.main_agent_name.trim()))) {
    console.error('Main agent name must contain 1 to 100 printable characters, without a leading --.'); process.exit(2);
  }
  if (typeof info.main_agent_id !== 'string' || !info.main_agent_id.trim() || !['codex', 'claude'].includes(info.engine)) {
    console.error('Connection needs a valid main_agent_id and engine; rerun worker setup.'); process.exit(2);
  }
}
const registry = join(home, 'computers.json');
if (existsSync(registry)) {
  const value = JSON.parse(readFileSync(registry, 'utf8'));
  if (!value.computers || typeof value.computers !== 'object') throw new Error('Existing registry is invalid; no changes made');
}
mkdirSync(join(home, 'tokens'), { recursive: true });
if (!existsSync(registry)) writeFileSync(registry, JSON.stringify({ computers: {} }, null, 2) + '\n', { mode: 0o600 });
// fleet.mjs lets only sessions in this folder (the orchestrator session) talk to main agents.
const saved = JSON.parse(readFileSync(registry, 'utf8'));
if (saved.orchestrator_workspace !== workspace) {
  saved.orchestrator_workspace = workspace;
  writeFileSync(registry, JSON.stringify(saved, null, 2) + '\n');
}
if (connection) {
  const root = dirname(dirname(fileURLToPath(import.meta.url)));
  const result = spawnSync(process.execPath, [join(root, 'skills/call-agent/fleet.mjs'), 'add-computer', connection, ...(local ? ['--local=1'] : [])],
    { env: { ...process.env, AGENT_FLEET_HOME: home }, stdio: 'inherit', windowsHide: true });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status || 1);
}
const desktop = await ensureDesktopProject({workspace,disabled:noDesktop});
console.log(JSON.stringify({ status: desktop.status === 'pending' ? 'desktop-setup-pending' : 'ready', desktop, registry, registered_workers: Object.keys(JSON.parse(readFileSync(registry, 'utf8')).computers) }, null, 2));
