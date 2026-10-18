"""Set up this Mac as a managed computer (worker) in the agent fleet; install-worker.ps1 does this on Windows.

Run it through install-worker.sh, as the user who normally signs in (no sudo):

    bash install-worker.sh [--connectivity local|tailscale|lan] [--full-access] [--language zh|en] ...

Safe to rerun: the token, the main agent's id and instructions, and existing data are kept. Only the
standard library is used.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import socket
import subprocess
import time
import sys
import urllib.request
import uuid

WORKER = Path(__file__).resolve().parent
LAUNCH_AGENT = 'com.agentfleet.worker'
EXTRA_PATH = ['/opt/homebrew/bin', '/usr/local/bin', str(Path.home() / '.local' / 'bin')]
TAILSCALE_APP = '/Applications/Tailscale.app/Contents/MacOS/Tailscale'
CLAUDE_MIN = (2, 1, 248)


class Setup:
    def __init__(self, args):
        self.args = args
        self.warnings = []
        self.data = Path(args.data_dir).expanduser().resolve()
        self.config_path = self.data / 'config.json'
        self.prior = json.loads(self.config_path.read_text(encoding='utf-8-sig')) if self.config_path.exists() else None
        language = args.language or (self.prior['language'] if self.prior else
                                     ('zh' if (os.environ.get('LANG') or system_language()).startswith('zh') else 'en'))
        self.language = language
        os.environ['PATH'] = os.pathsep.join([os.environ.get('PATH', '')] + EXTRA_PATH)

    def t(self, zh, en):
        return en if self.language == 'en' else zh

    def step(self, zh, en):
        if not self.args.json:
            print('==> ' + self.t(zh, en), flush=True)

    def fail(self, zh, en):
        sys.exit(self.t(zh, en))

    def run(self, *command, check=True, quiet=False, **options):
        result = subprocess.run(command, capture_output=quiet or self.args.json, text=True, **options)
        if check and result.returncode:
            raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)
        return result

    # --- settings -----------------------------------------------------------------------------------
    def resolve(self):
        a, prior = self.args, self.prior or {}
        if getattr(a, 'engines', None) and a.engine:
            self.fail('不要同时指定 --engine 和 --engines。', 'Pass --engine or --engines, not both.')
        self.engines = (a.engines.split(',') if getattr(a, 'engines', None) else [a.engine] if a.engine else
                        prior.get('engines') or ['claude' if prior.get('executor') == 'claude' else 'codex'])
        if not self.engines or len(set(self.engines)) != len(self.engines) or any(e not in ('codex', 'claude') for e in self.engines):
            self.fail('引擎必须为 codex、claude 或两个不同引擎。', 'Engines must be codex, claude or both distinct engines.')
        self.engine = self.engines[0]
        if getattr(a, 'second_main_agent_name', None) and len(self.engines) < 2:
            self.fail('第二个名字需要两个引擎。', 'A second main agent name requires two engines.')
        self.prior_connectivity = ('lan' if prior.get('lan_url') else
                                   'tailscale' if prior.get('remote_url') and not prior.get('local_only') else 'local')
        self.connectivity = a.connectivity or self.prior_connectivity
        self.workspace = Path(a.workspace or (Path(prior['tasks_dir']).parent if prior.get('tasks_dir') else Path.home() / 'AgentFleet')).expanduser().resolve()
        folders = prior.get('codex_app_folders')
        task_name = (folders.get('other') if isinstance(folders, dict) else None) or self.t('AgentFleet-其他', 'AgentFleet-Other')
        if task_name in ('.', '..') or re.search(r'[<>:"/\\|?*\x00-\x1f]', task_name):
            self.fail('任务项目名称不能包含路径分隔符或无效字符。', 'The task project name cannot contain path separators or invalid characters.')
        self.tasks_dir = self.workspace / task_name
        if prior.get('tasks_dir') and Path(prior['tasks_dir']).name != task_name:
            self.fail('任务目录名称必须与配置的项目名称一致，不支持自动迁移。',
                      'Task directory name must match the configured project name; automatic migration is not supported.')
        self.name = a.name or prior.get('computer_name') or default_name()
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', self.name):
            self.fail('电脑名只能用字母、数字、点、下划线和连字符，并以字母或数字开头。',
                      'The computer name must use letters, numbers, dots, underscores or hyphens, beginning with a letter or number.')
        self.main_name = (a.main_agent_name or prior.get('main_agent_name') or (self.t('Claude主agent', 'Claude main agent') if self.engine == 'claude' else self.t('主agent export', 'Main agent'))).strip()
        if not valid_name(self.main_name):
            self.fail('主 Agent 名字要 1 到 100 个可打印字符，不能以 -- 开头。', 'Main agent name must contain 1 to 100 printable characters, without a leading --.')
        self.manager_name = (a.manager_name or '').strip()
        if len(self.manager_name) > 100 or re.search(r'[\x00-\x1f\x7f]', self.manager_name):
            self.fail('管理名最多 100 个可打印字符。', 'The management name must contain at most 100 printable characters.')
        self.full_access = a.full_access or (prior.get('sandbox') == 'danger-full-access' and not a.restricted)
        self.port = a.port or prior.get('port') or 8765
        self.tailscale_port = a.tailscale_port or prior.get('tailscale_port') or 8766
        self.lan_address = ''
        if self.connectivity == 'lan':
            self.lan_address = a.lan_address or (prior.get('lan_address') if not a.connectivity else '') or detect_lan_address()
            try:
                parsed = ipaddress.IPv4Address(self.lan_address)
            except ValueError:
                parsed = None
            if not parsed or parsed.is_loopback:
                self.fail('找不到这台 Mac 的局域网 IPv4 地址，请用 --lan-address 指定。',
                          "Could not find this Mac's local network IPv4 address; pass --lan-address.")
        elif a.lan_address:
            self.fail('--lan-address 需要 --connectivity lan。', '--lan-address requires --connectivity lan.')
        self.mains = []
        profile_file = self.data / 'state' / 'profile.json'
        saved_profile = json.loads(profile_file.read_text(encoding='utf-8-sig')) if profile_file.exists() else {}
        for index, engine in enumerate(self.engines):
            old = next((m for m in prior.get('main_agents', []) if m['engine'] == engine), {})
            if not old and not prior.get('main_agents') and engine == ('claude' if prior.get('executor') == 'claude' else 'codex'):
                old = {'name': prior.get('main_agent_name'), 'id': prior.get('main_agent_id'),
                       'workspace': next(iter(prior.get('main_agent_workspaces', [])), None)}
            name = (a.main_agent_name if index == 0 else getattr(a, 'second_main_agent_name', None)) or old.get('name') or (
                self.t('Claude主agent', 'Claude main agent') if engine == 'claude' else self.t('主agent export', 'Main agent'))
            name = name.strip()
            if not valid_name(name) or any(m['name'] == name for m in self.mains):
                self.fail('主 Agent 名字无效或重复。', 'Invalid or duplicate main agent name.')
            if name != old.get('name') and name in saved_profile.get('agents', {}):
                self.fail('主 Agent 名字与现有 Agent 冲突。', 'Main agent name conflicts with an existing agent.')
            folder = old.get('workspace') or str(self.workspace / ('main-agent' if len(self.engines) == 1 else 'main-' + engine))
            saved = next((m for m in saved_profile.get('main_agents', []) if m['engine'] == engine), {})
            legacy_engine = 'claude' if prior.get('executor') == 'claude' else 'codex'
            legacy_id = saved_profile.get('main_agent_id') if not prior.get('main_agents') and engine == legacy_engine else None
            identity = saved.get('id') or legacy_id or old.get('id') or str(uuid.uuid4())
            self.mains.append({'name': name, 'engine': engine, 'id': identity, 'workspace': folder})
        self.main_name = self.mains[0]['name']
        self.main_workspace = Path(self.mains[0]['workspace'])

    def plan(self):
        return {'platform': 'macos', 'engine': self.engine, 'engines': self.engines, 'main_agents': self.mains, 'connectivity': self.connectivity, 'lan_address': self.lan_address,
                'computer': self.name, 'main_agent_name': self.main_name, 'manager_name': self.manager_name, 'language': self.language,
                'workspace': str(self.workspace), 'tasks_dir': str(self.tasks_dir), 'data_dir': str(self.data), 'full_access': self.full_access,
                'existing': bool(self.prior), 'autostart': not self.args.no_autostart}

    # --- steps --------------------------------------------------------------------------------------
    def prerequisites(self):
        def ensure(command, formula, cask=False):
            if shutil.which(command) or self.args.skip_prerequisites:
                return
            if not shutil.which('brew'):
                self.fail(f'缺少 {command}，也没有 Homebrew。请先安装它，再加 --skip-prerequisites 重新运行。',
                          f'{command} is missing and Homebrew is not available. Install it yourself, then rerun with --skip-prerequisites.')
            self.run('brew', 'install', *(['--cask'] if cask else []), formula)
        self.step('检查 Node.js 和 Tailscale', 'Checking Node.js and Tailscale')
        ensure('node', 'node')
        if self.connectivity == 'tailscale' and not (shutil.which('tailscale') or Path(TAILSCALE_APP).exists()):
            ensure('tailscale', 'tailscale', cask=True)
        if not shutil.which('node'):
            self.fail('找不到 Node.js', 'Node.js not found')

    def agent_cli(self):
        a = self.args
        self.codex = self.claude = self.claude_version = None
        if 'codex' in self.engines:
            self.step(f'准备 Codex 命令行 {a.codex_version}', f'Preparing the Codex CLI {a.codex_version}')
            self.codex = a.codex_executable or shutil.which('codex')
            installed = self.version(self.codex) if self.codex else None
            if not a.codex_executable and installed != a.codex_version:
                self.run('npm', 'install', '-g', f'@openai/codex@{a.codex_version}')
                prefix = self.run('npm', 'prefix', '-g', quiet=True).stdout.strip()
                self.codex = str(Path(prefix) / 'bin' / 'codex') if (Path(prefix) / 'bin' / 'codex').exists() else shutil.which('codex')
            if not self.codex:
                self.fail('安装 Codex 命令行失败', 'Installing the Codex CLI failed')
            self.codex = str(Path(self.codex).resolve()) if not a.codex_executable else self.codex
            if self.run(self.codex, 'login', 'status', check=False, quiet=True).returncode:
                self.warnings.append(self.t('Codex 还没有登录：运行 codex login 用你的 ChatGPT 账号登录，否则任务会失败。',
                                            'Codex is not signed in: run codex login with your ChatGPT account, or tasks will fail.'))
        if 'claude' in self.engines:
            self.step('准备 Claude Code 命令行', 'Preparing the Claude Code CLI')
            self.claude = a.claude_executable or shutil.which('claude')
            if not self.claude:
                script = self.data / 'install-claude.sh'
                self.data.mkdir(parents=True, exist_ok=True)
                urllib.request.urlretrieve('https://claude.ai/install.sh', script)
                self.run('bash', str(script))
                self.claude = shutil.which('claude') or str(Path.home() / '.local' / 'bin' / 'claude')
            if not Path(self.claude).exists():
                self.fail('找不到 claude；可用 --claude-executable 指定路径', 'claude was not found; pass its path with --claude-executable')
            version = self.version(self.claude) or ''
            if tuple(int(x) for x in version.split('.')[:3] if x.isdigit()) < CLAUDE_MIN:
                self.fail('Claude Code 需要 2.1.248 或更新版本以支持 restricted 模式，请先升级',
                          'Claude Code 2.1.248 or newer is needed for restricted mode; upgrade it first')
            self.claude_version = version
            if self.run(self.claude, 'auth', 'status', check=False, quiet=True).returncode:
                self.warnings.append(self.t('Claude Code 还没有登录：请本人运行 claude auth login，然后重新运行安装。',
                                            'Claude Code is not signed in: the owner must run claude auth login, then rerun this setup.'))

    def version(self, executable):
        try:
            text = self.run(executable, '--version', check=False, quiet=True).stdout
        except OSError:
            return None
        found = re.search(r'(\d+\.\d+\.\d+)', text or '')
        return found.group(1) if found else None

    def python_environment(self):
        self.step('准备 Python 环境', 'Preparing the Python environment')
        for folder in (self.data / 'state', self.tasks_dir, self.workspace / 'shared'):
            folder.mkdir(parents=True, exist_ok=True)
        venv = self.data / 'venv'
        self.python = venv / 'bin' / 'python'
        if not self.python.exists():
            # Some Python builds (e.g. minimal ones from version managers) cannot set up pip in a venv; try the others.
            names = ['python3.14', 'python3.13', 'python3.12', 'python3.11', 'python3.10', 'python3']
            candidates = dict.fromkeys([sys.executable] + [shutil.which(n) for n in names if shutil.which(n)] +
                                       [f'/opt/homebrew/bin/{n}' for n in names] + [f'/usr/local/bin/{n}' for n in names])
            for candidate in candidates:
                if not Path(candidate).exists() or self.run(candidate, '-c', 'import sys; sys.exit(sys.version_info < (3, 10))',
                                                            check=False, quiet=True).returncode:
                    continue
                if self.run(candidate, '-m', 'venv', str(venv), check=False, quiet=True).returncode == 0:
                    break
                shutil.rmtree(venv, ignore_errors=True)
            else:
                self.fail('创建 Python 环境失败（需要能创建 venv 的 Python 3.10 以上，例如 python.org 或 Homebrew 的版本）',
                          'Creating the Python environment failed (needs a Python 3.10+ that can create a venv, e.g. from python.org or Homebrew)')
        self.run(str(self.python), '-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '-r', str(WORKER / 'requirements.txt'))

    def token(self):
        self.token_file = self.data / 'token.txt'
        if self.token_file.exists():
            self.token_value = self.token_file.read_text(encoding='utf-8').strip()
        else:
            self.token_value = secrets.token_urlsafe(48)
            write_private(self.token_file, self.token_value)

    def tailscale(self):
        self.tailscale_executable = shutil.which('tailscale') or (TAILSCALE_APP if Path(TAILSCALE_APP).exists() else '')
        self.ts_name = self.ts_ip = None
        if self.connectivity == 'tailscale' and self.tailscale_executable:
            try:
                status = json.loads(self.run(self.tailscale_executable, 'status', '--json', check=False, quiet=True).stdout or '{}')
                if status.get('Self') and status.get('BackendState') == 'Running':
                    self.ts_name = status['Self'].get('DNSName', '').rstrip('.')
                    self.ts_ip = (status['Self'].get('TailscaleIPs') or [None])[0]
            except (OSError, ValueError):
                pass
        if self.connectivity == 'tailscale' and not self.ts_name:
            self.warnings.append(self.t('Tailscale 还没有登录：登录后重新运行本脚本，其他电脑才能连到这台。',
                                        'Tailscale is not signed in: sign in, then rerun this script so other computers can reach this one.'))

    def main_agent(self):
        for main in self.mains:
            self.engine, self.main_name, self.main_workspace = main['engine'], main['name'], Path(main['workspace'])
            self.write_main_agent()
        self.engine, self.main_name, self.main_workspace = self.engines[0], self.mains[0]['name'], Path(self.mains[0]['workspace'])

    def write_main_agent(self):
        self.step(f'创建主 Agent：{self.main_name}', f'Creating the main agent: {self.main_name}')
        (self.main_workspace / '.codex').mkdir(parents=True, exist_ok=True)
        agents = self.main_workspace / 'AGENTS.md'
        if not agents.exists():
            template = (WORKER / 'templates' / 'main-agent' / self.t('AGENTS.md', 'AGENTS.en.md')).read_text(encoding='utf-8')
            note = (self.t('子任务以完整权限运行，可操作桌面、浏览器和其他应用；所有权限请求自动批准。',
                           'Sub-tasks run with full access and can operate the desktop, browsers and other apps; every permission request is approved automatically.')
                    if self.full_access else
                    self.t('子任务使用 Claude Code restricted 模式：文件工具限于工作目录和 shared 文件夹，命令执行关闭；这不是操作系统级沙箱。',
                           'Sub-tasks use Claude Code restricted mode: file tools are limited to the working folder and the shared folder, and command execution is off. This is not an OS-level sandbox.')
                    if self.engine == 'claude' else
                    self.t('子任务使用 Codex workspace-write 沙箱，不自动批准额外权限。',
                           'Sub-tasks use the Codex workspace-write sandbox; extra permissions are not approved automatically.'))
            text = template.replace('\\', '/')  # the template's folder references use Windows separators
            for key, value in (('COMPUTER_NAME', self.name), ('WORKSPACE', str(self.workspace)), ('AGENT_ENGINE', self.engine),
                               ('MAIN_AGENT_NAME', self.main_name), ('MAIN_AGENT_WORKSPACE', str(self.main_workspace)), ('TASKS_DIR_NAME', self.tasks_dir.name), ('PERMISSION_NOTE', note)):
                text = text.replace('{{' + key + '}}', value)
            agents.write_text(text, encoding='utf-8')
        if not (self.main_workspace / 'CLAUDE.md').exists():
            shutil.copyfile(agents, self.main_workspace / 'CLAUDE.md')
        if self.engine == 'codex':
            write_private(self.main_workspace / '.codex' / 'config.toml', '\n'.join([
                self.t('# 主 Agent 专用：只在本项目里启用派任务服务，子任务不会加载它。文件里含有访问令牌，不要分享。',
                       '# For the main agent only: the dispatcher is enabled in this project alone, and sub-tasks do not load it. This file holds an access token; do not share it.'),
                '[mcp_servers.codex_tasks]', f'url = "http://127.0.0.1:{self.port}/mcp"',
                f'http_headers = {{ Authorization = "Bearer {self.token_value}" }}', 'tool_timeout_sec = 120',
                self.t('# 后台运行时没人点“批准”，预先批准这个服务的工具调用（只对本项目生效）',
                       "# Nobody is there to click \"approve\" in the background, so this service's tool calls are pre-approved (this project only)"),
                'default_tools_approval_mode = "approve"', '']))
            # Codex only loads a project's own config when the project is trusted.
            codex_config = Path(self.args.codex_home).expanduser() / 'config.toml'
            codex_config.parent.mkdir(parents=True, exist_ok=True)
            existing = codex_config.read_text(encoding='utf-8') if codex_config.exists() else ''
            if f"[projects.'{self.main_workspace}']" not in existing:
                with codex_config.open('a', encoding='utf-8') as file:
                    file.write(f"\n[projects.'{self.main_workspace}']\ntrust_level = \"trusted\"\n")
        rules = self.workspace / 'worker-rules.md'
        if not rules.exists():
            shutil.copyfile(WORKER / 'templates' / self.t('worker-rules.md', 'worker-rules.en.md'), rules)

    def write_config(self):
        self.step('写入本机配置', "Writing this computer's configuration")
        main_id = self.mains[0]['id']
        hosts = [f'127.0.0.1:{self.port}', f'localhost:{self.port}']
        if self.ts_name:
            hosts.append(f'{self.ts_name}:{self.tailscale_port}')
        if self.ts_ip:
            hosts.append(f'{self.ts_ip}:{self.tailscale_port}')
        if self.lan_address:
            hosts.append(f'{self.lan_address}:{self.port}')
        self.remote_url = f'http://{self.ts_name}:{self.tailscale_port}/mcp' if self.ts_name else ''
        self.lan_url = f'http://{self.lan_address}:{self.port}/mcp' if self.lan_address else ''
        config = {
            'computer_name': self.name, 'main_agent_name': self.main_name, 'main_agent_id': main_id, 'language': self.language,
            'port': self.port, 'tailscale_port': self.tailscale_port, 'remote_url': self.remote_url,
            'bind_host': '0.0.0.0' if self.lan_address else '127.0.0.1', 'lan_address': self.lan_address, 'lan_url': self.lan_url,
            'python_executable': str(self.python), 'codex_executable': self.codex,
            # Where node and the CLIs live: the service may later start from launchd with a minimal PATH.
            'service_path': list(dict.fromkeys(str(Path(p).parent) for p in (shutil.which('node'), shutil.which('codex'), shutil.which('claude')) if p)), 'codex_launcher_executable': self.codex,
            'codex_launcher_args': [], 'codex_version': self.args.codex_version if 'codex' in self.engines else None,
            'tailscale_executable': self.tailscale_executable, 'state_dir': str(self.data / 'state'),
            'token_file': str(self.token_file), 'worker_rules_file': str(self.workspace / 'worker-rules.md'),
            'tasks_dir': str(self.tasks_dir), 'shared_dir': str(self.workspace / 'shared'),
            'allowed_roots': [str(self.workspace)], 'allowed_hosts': hosts, 'main_agent_workspaces': [str(self.main_workspace)],
            'executor': 'claude' if self.engine == 'claude' else 'app-server', 'engine': self.engine,
            'engines': self.engines, 'main_agents': self.mains,
            'claude_executable': self.claude or '', 'local_only': self.connectivity != 'tailscale',
            'sandbox': 'danger-full-access' if self.full_access else 'workspace-write', 'auto_approve': self.full_access,
            'max_timeout_seconds': 7200, 'writer_wait_seconds': 10, 'fork_when_busy': True,
        }
        if self.claude_version:
            config['claude_version'] = self.claude_version
        # Keep options the user added (quota, display preferences); the codex_app_folders default is added once.
        for key, value in (self.prior or {}).items():
            config.setdefault(key, value)
        if 'codex' in self.engines:
            config.setdefault('codex_app_folders', {'main': 'AgentFleet', 'other': self.t('AgentFleet-其他', 'AgentFleet-Other')})
        if self.config_path.exists():
            shutil.copyfile(self.config_path, self.config_path.with_suffix('.json.bak'))
        self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        self.config = config

    def autostart(self):
        if self.args.no_autostart:
            return
        self.step('设置登录后自动启动', 'Adding autostart at login')
        plist = Path.home() / 'Library' / 'LaunchAgents' / f'{LAUNCH_AGENT}.plist'
        plist.parent.mkdir(parents=True, exist_ok=True)
        # A login item runs inside the user's session, which agents need to control the screen. The service
        # starts in its own process group, so it keeps running after this short-lived job exits.
        arguments = [str(self.python), str(WORKER / 'service_ctl.py'), 'start', '--config', str(self.config_path)]
        if self.connectivity != 'tailscale':
            arguments.append('--local-only')
        with plist.open('wb') as file:
            plistlib.dump({'Label': LAUNCH_AGENT, 'ProgramArguments': arguments, 'RunAtLoad': True, 'AbandonProcessGroup': True,
                           'StandardOutPath': str(self.data / 'state' / 'launchd.log'),
                           'StandardErrorPath': str(self.data / 'state' / 'launchd.log')}, file)
        self.plist = plist

    def restart(self):
        if self.args.no_start:
            return True
        self.step('启动服务', 'Starting the service')
        control = [str(self.python), str(WORKER / 'service_ctl.py')]
        answer = self.run(*control, 'status', '--config', str(self.config_path), check=False, quiet=True)
        if answer.returncode == 0:
            # Restart an idle service so it picks up the new code and config; never interrupt work.
            request = urllib.request.Request(f'http://127.0.0.1:{self.port}/api/tasks', headers={'Authorization': 'Bearer ' + self.token_value})
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    busy = any(t['status'] in ('queued', 'running') for t in json.loads(response.read())['tasks'])
            except (OSError, ValueError, KeyError):
                busy = True  # Unknown activity must also wait; only a confirmed idle worker may be stopped.
            if busy:
                # Typically this computer's own main agent runs setup, so the service is busy with that very turn.
                # The running service relaunches itself on the new code once its queue is empty.
                (self.data / 'state' / 'restart-when-idle').write_text(time.strftime('%Y-%m-%dT%H:%M:%S'), encoding='utf-8')
                self.warnings.append(self.t('服务正在执行任务；新代码已部署，任务全部结束后服务会自动重启生效。',
                                            'The service is busy; the new code is deployed and the service restarts itself once its tasks finish.'))
                return True
            self.run(*control, 'stop', '--config', str(self.config_path), check=False, quiet=True)
        started = self.run(*control, 'start', '--config', str(self.config_path),
                           *(['--local-only'] if self.connectivity != 'tailscale' or not self.ts_name else []), check=False, quiet=True)
        if started.returncode:
            self.warnings.append(self.t('服务没有正常启动，看 state/server.stderr.log。', 'The service did not start properly; see state/server.stderr.log.')
                                 + ' ' + (started.stderr or started.stdout or '').strip()[-300:])
            return False
        return True

    def connection(self):
        dashboard = (f'http://{self.ts_name}:{self.tailscale_port}/dashboard' if self.ts_name else
                     f'http://{self.lan_address}:{self.port}/dashboard' if self.lan_address else f'http://127.0.0.1:{self.port}/dashboard')
        info = {'name': self.name, 'engine': self.engine, 'engines': self.engines, 'main_agents': self.mains, 'description': self.args.description or '', 'local_url': f'http://127.0.0.1:{self.port}/mcp',
                'tailscale_url': self.remote_url, 'lan_url': self.lan_url, 'token': self.token_value,
                'main_agent_workspace': str(self.main_workspace), 'main_agent_name': self.main_name,
                'main_agent_id': self.config['main_agent_id'], 'manager_name': self.manager_name, 'language': self.language, 'dashboard': dashboard}
        self.connection_file = self.data / 'connection.json'
        write_private(self.connection_file, json.dumps(info, ensure_ascii=False, indent=2))
        self.dashboard = dashboard

    def pairing(self, started):
        if not started or self.args.no_start or not (self.lan_url or self.remote_url):
            return None
        # A rerun (e.g. an update) keeps its orchestrators; only a fresh install or new connectivity opens a window.
        if self.prior and self.connectivity == self.prior_connectivity:
            return None
        done = self.run(str(self.python), str(WORKER / 'tools' / 'pairing.py'), '--config', str(self.config_path), '--json', check=False, quiet=True)
        if done.returncode:
            self.warnings.append(self.t('没能打开配对窗口；稍后运行 worker/pair.sh 再试。', 'Could not open a pairing window; run worker/pair.sh later.'))
            return None
        return json.loads(done.stdout)

    def go(self):
        self.resolve()
        if self.args.plan:
            print(json.dumps(self.plan(), ensure_ascii=False, indent=2))
            return
        self.prerequisites()
        self.agent_cli()
        self.python_environment()
        self.token()
        self.tailscale()
        self.main_agent()
        self.write_config()
        self.autostart()
        started = self.restart()
        self.connection()
        pairing = self.pairing(started)
        result = {'status': 'service-ready' if started and not self.args.no_start else 'configured', **self.plan(),
                  'connection_file': str(self.connection_file), 'dashboard': self.dashboard, 'remote_url': self.remote_url,
                  'lan_url': self.lan_url, 'pairing': pairing, 'pair_script': str(WORKER / 'pair.sh'),
                  'config': str(self.config_path), 'warnings': self.warnings}
        if self.args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        print()
        print(self.t(f'完成：{self.name} 已接入。', f'Done: {self.name} is connected.'))
        print(self.t(f'接入信息：{self.connection_file}（含令牌，只发给你自己的管理端电脑）',
                     f'Connection info: {self.connection_file} (contains the token; send it only to your own orchestrator computer)'))
        print(self.t(f'只读看板：{self.dashboard}', f'Read-only dashboard: {self.dashboard}'))
        if pairing:
            print(self.t(f"配对码：{pairing['code']}（{pairing['expires_in'] // 60} 分钟内有效，只能用一次）。在管理端对 Claude 说\"搜索并配对新电脑\"，再告诉它这个配对码。",
                         f"Pairing code: {pairing['code']} (valid {pairing['expires_in'] // 60} minutes, works once). On the orchestrator, ask Claude to \"search for new computers and pair\" and give it this code."))
            print(self.t('配对码过期了就运行 worker/pair.sh 重新生成。', 'If the code expires, run worker/pair.sh for a new one.'))
        if self.lan_address:
            print(self.t(f'局域网地址：{self.lan_url}（明文 HTTP，只在可信网络使用）。如果 macOS 问是否允许 Python 接受传入连接，请点"允许"。',
                         f'LAN address: {self.lan_url} (plain HTTP, trusted networks only). If macOS asks whether Python may accept incoming connections, click Allow.'))
        was_full = (self.prior or {}).get('sandbox') == 'danger-full-access'
        mode = self.t('完全访问', 'full access') if self.full_access else self.t('受限', 'restricted')
        if self.prior and was_full != self.full_access:
            print(self.t(f'子任务权限：已改为{mode}（{self.config_path}）', f'Sub-task permissions: changed to {mode} ({self.config_path})'))
        else:
            print(self.t(f'子任务权限：{mode}', f'Sub-task permissions: {mode}'))
        if self.full_access:
            print(self.t('要让子任务操作桌面，macOS 会要求给 Codex/终端"辅助功能"和"屏幕录制"权限，请在 系统设置 → 隐私与安全性 里允许。',
                         'For sub-tasks to operate the desktop, macOS asks for Accessibility and Screen Recording permission for Codex/Terminal; allow them in System Settings → Privacy & Security.'))
        else:
            print(self.t('当前子任务只能写自己的工作目录。要操作桌面和浏览器，用 --full-access 重新运行。',
                         'Sub-tasks can currently write only their own working folder. To let them operate the desktop and browsers, rerun with --full-access.'))
        for warning in self.warnings:
            print('WARNING: ' + warning, file=sys.stderr)


def system_language():
    try:
        return subprocess.run(['defaults', 'read', '-g', 'AppleLocale'], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def default_name():
    try:
        name = subprocess.run(['scutil', '--get', 'LocalHostName'], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ''
    name = re.sub(r'[^A-Za-z0-9_.-]', '-', (name or socket.gethostname().split('.')[0]).lower()).strip('-.') or 'mac'
    return name[:100]


def valid_name(name):
    return 1 <= len(name) <= 100 and not re.search(r'[\x00-\x1f\x7f]', name) and not name.startswith('--') \
        and name not in ('__proto__', 'constructor', 'prototype')


def detect_lan_address():
    """The IPv4 address of the interface that carries the default route, i.e. the one other computers reach."""
    try:
        route = subprocess.run(['route', '-n', 'get', 'default'], capture_output=True, text=True, timeout=5).stdout
        interface = re.search(r'interface:\s*(\S+)', route).group(1)
        return subprocess.run(['ipconfig', 'getifaddr', interface], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError, AttributeError):
        return ''


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(text, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--engines')
    parser.add_argument('--second-main-agent-name')
    parser.add_argument('--engine', choices=['codex', 'claude'])
    parser.add_argument('--connectivity', choices=['local', 'tailscale', 'lan'])
    parser.add_argument('--lan-address', default='')
    parser.add_argument('--workspace')
    parser.add_argument('--data-dir', default=str(Path.home() / 'Library' / 'Application Support' / 'agent-fleet'))
    parser.add_argument('--name')
    parser.add_argument('--main-agent-name')
    parser.add_argument('--manager-name', default='')
    parser.add_argument('--language', choices=['zh', 'en'])
    parser.add_argument('--description', default='')
    parser.add_argument('--full-access', action='store_true', help='sub-tasks run without a sandbox and every permission request is approved')
    parser.add_argument('--restricted', action='store_true', help='turn full access off again on a rerun')
    # Accepted so the same command works on Windows and macOS; on macOS a rerun always applies the options given.
    parser.add_argument('--reconfigure', action='store_true', help='apply changed settings to an existing worker')
    parser.add_argument('--port', type=int)
    parser.add_argument('--tailscale-port', type=int)
    parser.add_argument('--codex-version', default='0.162.1')
    parser.add_argument('--codex-executable', help='use this Codex CLI instead of installing one')
    parser.add_argument('--claude-executable')
    parser.add_argument('--codex-home', default=os.environ.get('CODEX_HOME') or str(Path.home() / '.codex'))
    parser.add_argument('--skip-prerequisites', action='store_true', help='do not install Node.js or Tailscale with Homebrew')
    parser.add_argument('--no-autostart', action='store_true')
    parser.add_argument('--no-start', action='store_true')
    parser.add_argument('--plan', action='store_true', help='print the resolved settings and change nothing')
    parser.add_argument('--json', action='store_true', help='print the result as JSON (for setup scripts)')
    if sys.platform != 'darwin':
        sys.exit('install_mac.py is for macOS; on Windows use install-worker.ps1.')
    try:
        Setup(parser.parse_args()).go()
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or '').strip()[-1500:]
        sys.exit(f"Failed: {' '.join(map(str, error.cmd))}\n{detail}")


if __name__ == '__main__':
    main()
