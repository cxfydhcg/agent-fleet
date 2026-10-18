import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { appDataRedirected, runOutsideApp, windowsDataDir } from './windows-outside.mjs';

// Use the installed runtime: a plugin cache can be replaced independently of the worker.
export async function startWorker({ configPath, platform = process.platform, env = process.env,
  home = homedir(), run = spawnSync, request = fetch, redirected, runOutside = runOutsideApp } = {}) {
  if (!['darwin', 'win32'].includes(platform)) {
    throw new Error('Worker startup supports macOS and Windows. / 工作端启动支持 macOS 和 Windows。');
  }
  configPath = resolve(configPath || env.AGENT_FLEET_CONFIG || (platform === 'darwin'
    ? join(home, 'Library', 'Application Support', 'agent-fleet', 'config.json')
    : join(windowsDataDir(home), 'config.json')));
  let config;
  try { config = JSON.parse(readFileSync(configPath, 'utf8').replace(/^\uFEFF/, '')); }
  catch { throw new Error('Worker config missing or invalid; use Agent Fleet setup. / 配置缺失或无效，请先运行 Agent Fleet setup。'); }
  const t = (zh, en) => config.language === 'zh' ? zh : en;
  if (typeof config.tasks_dir !== 'string' || !isAbsolute(config.tasks_dir)) {
    throw new Error(t('工作目录配置无效，请运行 setup 修复。', 'Invalid workspace configuration; repair with setup.'));
  }
  const port = config.port ?? 8765;
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(t('服务端口配置无效。', 'Invalid service port.'));
  }
  const runtime = join(dirname(config.tasks_dir), 'service', 'worker');
  const script = join(runtime, platform === 'darwin' ? 'start.sh' : 'start.ps1');
  if (!existsSync(script)) {
    throw new Error(t('已安装的启动脚本缺失，请运行 setup 修复。', 'Installed startup script missing; repair with setup.'));
  }
  let token;
  try { token = readFileSync(config.token_file, 'utf8').trim(); }
  catch { throw new Error(t('无法读取本机服务凭证，请运行 setup 修复。', 'Cannot read the local service credential; repair with setup.')); }
  if (!token) throw new Error(t('本机服务凭证为空。', 'The local service credential is empty.'));
  const executable = platform === 'darwin' ? '/bin/bash' : 'powershell.exe';
  const args = platform === 'darwin' ? [script, '--config', configPath]
    : ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script, '-ConfigPath', configPath];
  // Started from inside the Codex or Claude desktop app, the service would live in its sandbox and stop with it.
  const outside = platform === 'win32' && (redirected ?? appDataRedirected({ home, platform, env }));
  const result = outside ? runOutside(executable, args, { home, timeoutMs: 120000 })
    : run(executable, args, { env, cwd: runtime, encoding: 'utf8', timeout: 90000, windowsHide: true });
  // Do not echo captured output: a local configuration or launcher error can contain credentials.
  if (result.error || result.status !== 0) {
    throw new Error(t('启动失败；请检查数据目录 state 内的服务日志。', 'Startup failed; check service logs under the data directory’s state folder.'));
  }
  try {
    const response = await request(`http://127.0.0.1:${port}/health`, {
      headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(5000),
    });
    if (!response.ok || (await response.json()).service !== 'codex-task-mcp') throw new Error();
  } catch {
    throw new Error(t('服务尚未就绪；请检查数据目录 state 内的服务日志。', 'Service is not ready; check service logs under the data directory’s state folder.'));
  }
  return { status: 'ready', computer: config.computer_name,
    dashboard: `http://127.0.0.1:${port}/dashboard` };
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length === 1 && ['--help', '-h'].includes(args[0])) {
    console.log('Start the installed worker / 启动已安装的工作端\nUsage: node start.mjs [--config PATH]');
    return;
  }
  if (args.length && !(args.length === 2 && args[0] === '--config' && args[1] && !args[1].startsWith('--'))) {
    throw new Error('Usage: node start.mjs [--config PATH]');
  }
  console.log(JSON.stringify(await startWorker({ configPath: args[1] }), null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
