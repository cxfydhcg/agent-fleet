"""Start, stop or check this computer's worker service on macOS (start.ps1 / stop.ps1 do this on Windows).

    python service_ctl.py start|stop|status [--config <config.json>] [--local-only]

start: launch the service if it is not answering, wait until it is ready, then publish it on Tailscale when
the config says so. stop: stop the service process the health check names, with its agent processes, and
remove the Tailscale route. Only the standard library is used.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = Path.home() / 'Library' / 'Application Support' / 'agent-fleet' / 'config.json'


def health(config):
    token = Path(config['token_file']).read_text(encoding='utf-8').strip()
    request = urllib.request.Request(f"http://127.0.0.1:{config.get('port', 8765)}/health",
                                     headers={'Authorization': 'Bearer ' + token})
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            answer = json.loads(response.read())
    except (OSError, ValueError):
        return None
    return answer if answer.get('service') == 'codex-task-mcp' else None


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def tailscale(config, *args, check=True):
    return subprocess.run([config['tailscale_executable'], *args], capture_output=True, text=True, check=check, timeout=30)


def publish_on_tailscale(config):
    executable, port, local = config.get('tailscale_executable'), config.get('tailscale_port', 8766), config.get('port', 8765)
    if not executable or not Path(executable).exists():
        sys.exit('Tailscale executable not found. Local service is ready.')
    status = json.loads(tailscale(config, 'serve', 'status', '--json').stdout or '{}')
    if str(port) in (status.get('TCP') or {}):
        routes = [route for host, route in (status.get('Web') or {}).items() if host.endswith(f':{port}')]
        if len(routes) != 1 or ((routes[0].get('Handlers') or {}).get('/') or {}).get('Proxy') != f'http://127.0.0.1:{local}':
            sys.exit('Tailscale port is already used by another service; no changes made to it.')
    tailscale(config, 'serve', '--bg', f'--http={port}', '--yes', f'http://127.0.0.1:{local}')
    print(f"Tailscale MCP: {config.get('remote_url')}")


def start(config, config_path, local_only):
    if not health(config):
        launched = subprocess.run([config['python_executable'], str(HERE / 'start_service.py'), '--config', str(config_path)],
                                  capture_output=True, text=True, timeout=60)
        if launched.returncode or not launched.stdout.strip().isdigit():
            sys.exit('Service launcher failed: ' + (launched.stderr or launched.stdout).strip()[-500:])
        pid = int(launched.stdout.strip())
        for _ in range(40):
            time.sleep(0.25)
            if not alive(pid):
                sys.exit(f"Service exited. Check server.stderr.log in {config['state_dir']}.")
            if health(config):
                break
        else:
            os.kill(pid, signal.SIGTERM)
            sys.exit('Service did not become ready.')
    print(f"Local MCP: http://127.0.0.1:{config.get('port', 8765)}/mcp")
    if not local_only and not config.get('local_only') and config.get('remote_url'):
        publish_on_tailscale(config)


def stop(config):
    answer = health(config)
    if answer:
        pid = answer['pid']
        # Each agent process runs in its own process group under the service; stop those groups too.
        children = subprocess.run(['pgrep', '-P', str(pid)], capture_output=True, text=True).stdout.split()
        os.kill(pid, signal.SIGTERM)
        for _ in range(40):
            if not alive(pid):
                break
            time.sleep(0.25)
        for child in children:
            for kill in (lambda: os.killpg(int(child), signal.SIGKILL), lambda: os.kill(int(child), signal.SIGKILL)):
                try:
                    kill()
                    break
                except (ProcessLookupError, PermissionError):
                    continue
        if alive(pid):
            os.kill(pid, signal.SIGKILL)
    if not config.get('local_only') and config.get('remote_url') and Path(config.get('tailscale_executable') or '/nonexistent').exists():
        tailscale(config, 'serve', f"--http={config.get('tailscale_port', 8766)}", 'off', check=False)
    print('Codex task MCP stopped.')


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('action', choices=['start', 'stop', 'status'])
    parser.add_argument('--config', type=Path, default=Path(os.environ.get('AGENT_FLEET_CONFIG') or DEFAULT_CONFIG))
    parser.add_argument('--local-only', action='store_true', help='do not publish the service on Tailscale')
    args = parser.parse_args()
    config_path = args.config.resolve()
    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    if args.action == 'start':
        start(config, config_path, args.local_only)
    elif args.action == 'stop':
        stop(config)
    else:
        answer = health(config)
        print(json.dumps(answer or {'status': 'stopped'}))
        sys.exit(0 if answer else 1)


if __name__ == '__main__':
    main()
