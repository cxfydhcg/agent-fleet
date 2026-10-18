"""Start the service without inheriting an agent terminal's capture pipes."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time


def pid_alive(pid):
    if os.name == 'nt':
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def service_env(config, environ=None, posix=os.name != 'nt'):
    env = dict(os.environ if environ is None else environ)
    if posix:
        # launchd jobs and login items start with a minimal PATH (/usr/bin:/bin:...). The Codex and Claude CLIs are
        # often Node scripts from Homebrew or npm, so put back the directories setup found them in.
        extra = list(config.get('service_path') or []) + ['/opt/homebrew/bin', '/usr/local/bin', str(Path.home() / '.local' / 'bin')]
        env['PATH'] = ':'.join(dict.fromkeys(p for p in env.get('PATH', '').split(':') + extra if p))
    return env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--after-pid', type=int, help='wait for this process (the old service) to exit first')
    args = parser.parse_args()
    if args.after_pid:
        deadline = time.time() + 60
        while pid_alive(args.after_pid) and time.time() < deadline:
            time.sleep(0.5)
        time.sleep(1)  # let the port be released
    config = json.loads(args.config.read_text(encoding='utf-8'))
    state = Path(config['state_dir'])
    state.mkdir(parents=True, exist_ok=True)
    command = [config['python_executable'], '-u', str(Path(__file__).with_name('server.py')),
               '--config', str(args.config.resolve())]
    with (state / 'server.stdout.log').open('wb') as stdout, (state / 'server.stderr.log').open('wb') as stderr:
        # Detached from the caller: no console window on Windows, its own session elsewhere, so closing the
        # terminal (or a login script exiting) does not stop the service.
        detach = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, close_fds=True,
                                   env=service_env(config), **detach)
    print(process.pid)


if __name__ == '__main__':
    main()
