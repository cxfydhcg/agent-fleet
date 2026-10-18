// Windows apps from the Microsoft Store (the Codex and Claude desktop apps) run their tools in a sandbox:
// writes under AppData are redirected into the app's private storage (AppData\Local\Packages\<app>\LocalCache),
// and Codex also points LOCALAPPDATA there. A worker installed from inside would keep its config, token and
// Python environment in storage that the app's reset or uninstall deletes, and that autostart and other apps
// don't see. So the worker's data always lives in the user's real AppData\Local\agent-fleet, and when setup or
// start runs inside such a sandbox, the PowerShell part is launched outside it through WMI.
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

export const windowsDataDir = (home = homedir()) => join(home, 'AppData', 'Local', 'agent-fleet');

// True when this process's AppData writes land in an app's private storage.
export function appDataRedirected({ home = homedir(), platform = process.platform, env = process.env } = {}) {
  if (platform !== 'win32' || env.AGENT_FLEET_OUTSIDE === '0') return false;  // '0': tests and deliberate in-app runs
  if (/\\Packages\\[^\\]+\\LocalCache\\/i.test(env.LOCALAPPDATA || '')) return true;
  const local = join(home, 'AppData', 'Local'), name = `.agent-fleet-probe-${process.pid}-${Date.now()}`;
  const probe = join(local, name);
  try { mkdirSync(probe); } catch { return false; }
  try {
    const packages = join(local, 'Packages');
    return readdirSync(packages).some((app) => existsSync(join(packages, app, 'LocalCache', 'Local', name)));
  } catch { return false; }
  finally { try { rmSync(probe, { recursive: true, force: true }); } catch { /* best effort */ } }
}

const psQuote = (value) => `'${String(value).replace(/'/g, "''")}'`;

// Runs a command in a new process created by WMI, which is outside any app sandbox and not a child of the app,
// then waits for it. Output goes through a file in the user's home folder, which is never redirected.
export function runOutsideApp(executable, args, { home = homedir(), timeoutMs = 45 * 60 * 1000 } = {}) {
  const dir = join(home, '.agent-fleet-setup');
  mkdirSync(dir, { recursive: true });
  const id = `${process.pid}-${Date.now()}`;
  const script = join(dir, `${id}.ps1`), out = join(dir, `${id}.out`), done = join(dir, `${id}.done`);
  writeFileSync(script, '﻿' + [
    '$ErrorActionPreference = "Continue"',
    '[Console]::OutputEncoding = [Text.Encoding]::UTF8  # the console code page the installer inherits, so Chinese text survives',
    // Windows PowerShell 5.1 drops embedded double quotes when it passes arguments to a program; escape them.
    `& ${psQuote(executable)} ${args.map((a) => psQuote(String(a).replace(/"/g, '\\"'))).join(' ')} *>&1 | ForEach-Object { "$_" } | Out-File -LiteralPath ${psQuote(out)} -Encoding utf8`,
    `[IO.File]::WriteAllText(${psQuote(done)}, [string]$LASTEXITCODE)`,
  ].join('\r\n'), 'utf8');
  try {
    const launch = spawnSync('powershell', ['-NoProfile', '-Command',
      `$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = ${psQuote(`powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "${script}"`)} }; exit [int]$r.ReturnValue`],
      { encoding: 'utf8', windowsHide: true });
    if (launch.error || launch.status !== 0) throw new Error('Could not start the installer outside the app sandbox (WMI).');
    const wait = new Int32Array(new SharedArrayBuffer(4));
    for (const start = Date.now(); !existsSync(done); ) {
      if (Date.now() - start > timeoutMs) throw new Error('The installer outside the app sandbox did not finish in time.');
      Atomics.wait(wait, 0, 0, 500);
    }
    Atomics.wait(wait, 0, 0, 200);  // let Out-File flush
    const status = Number(readFileSync(done, 'utf8').trim() || 0);
    const stdout = existsSync(out) ? readFileSync(out, 'utf8').replace(/^﻿/, '') : '';
    return { status: Number.isFinite(status) ? status : 1, stdout, stderr: '' };
  } finally {
    for (const file of [script, out, done]) { try { rmSync(file, { force: true }); } catch { /* best effort */ } }
  }
}
