"""Private Streamable HTTP MCP bridge to the locally authenticated Codex CLI.

Codex tasks run through `codex app-server`, so their sessions show up in the Codex desktop app.
Claude tasks run through the Claude Code adapter.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hmac
import json
import os
import platform
from pathlib import Path
import re
import secrets
import shutil
import signal
import sys
import subprocess
import time
import uuid
from urllib.parse import quote

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
import uvicorn
import claude_executor
from tools.codex_app_folder import STATE_FILE, desktop_app_running, same_path, sort_into_folders, write_state


def now():
    return datetime.now(timezone.utc).isoformat()


def local_clock(timestamp):
    """A unix time as local HH:MM, with the date when it is not today."""
    moment = datetime.fromtimestamp(timestamp)
    return moment.strftime('%H:%M' if moment.date() == datetime.now().date() else '%m-%d %H:%M')


# Quota guard: wait this long after the reported reset, at most this long in one go, and re-check this often
# when Codex does not say when the window resets. Module constants, so tests can shrink them.
QUOTA_SLACK_SECONDS, QUOTA_MAX_WAIT_SECONDS, QUOTA_RECHECK_SECONDS, QUOTA_CHECK_TIMEOUT = 60, 6 * 3600, 600, 15
QUOTA_WINDOWS = ('primary', 'secondary')  # the 5-hour and the weekly window


def quota_exhausted(result, threshold, language='zh'):
    """Read an account/rateLimits/read answer: (labels of the windows at or over threshold percent, unix time
    the last of them resets or None). None when nothing is used up or the shape is unknown, so an
    unexpected answer never blocks work."""
    limits = result.get('rateLimits') if isinstance(result, dict) else None
    if not isinstance(limits, dict):
        return None
    number = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool)
    labels, resets = [], []
    for key in QUOTA_WINDOWS:
        window = limits.get(key) if isinstance(limits.get(key), dict) else {}
        if number(window.get('usedPercent')) and window['usedPercent'] >= threshold:
            labels.append(TEXT[language]['windows'][key])
            if number(window.get('resetsAt')):
                resets.append(window['resetsAt'])
    return (labels, max(resets) if resets else None) if labels else None


class QuotaExhausted(Exception):
    """The engine's quota is used up; raised before any session is started, so the job can simply wait its turn."""

    def __init__(self, labels, resets_at, language='zh', engine='Codex'):
        self.labels, self.resets_at, self.engine = labels, resets_at, engine
        text = TEXT[language]
        when = (text['quota_at'].format(time=local_clock(resets_at)) if resets_at else
                text['quota_poll'].format(minutes=max(1, round(QUOTA_RECHECK_SECONDS / 60))))
        super().__init__(text['quota_note'].format(engine=engine) +
                         text['quota_used'].format(labels=text['quota_joiner'].join(labels), when=when))



class AppServerError(RuntimeError):
    def __init__(self, error):
        super().__init__(error.get('message') or json.dumps(error, ensure_ascii=False))
        self.error = error


def task_folder_name(task_id, name=None):
    """A readable folder for a new task: date, name and short id, e.g. 2026-10-12_检查B站登录状态_a0bece4b."""
    label = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', ' ', name or '').strip(' .')[:40].strip()
    parts = [datetime.now().strftime('%Y-%m-%d')] + ([re.sub(r'\s+', '-', label)] if label else []) + [task_id[:8]]
    return '_'.join(parts)


# Pairing: how long a pairing code stays valid and how many wrong codes close the window.
PAIRING_SECONDS, PAIRING_ATTEMPTS = 600, 5
FINISHED = ('completed', 'failed', 'cancelled', 'interrupted', 'timed_out')
WORKER_RULES = '''你是被派出来执行具体任务的子 agent（worker）。必须遵守：
1. 只完成交给你的这件任务。做完后，把结果、遇到的问题和需要人处理的事写进最终回复，由派你来的上级（本机的主agent export）处理。
2. 不要调用本机的派任务服务（codex_tasks / Codex Task Dispatcher，地址如 127.0.0.1:8765 或 :8766/mcp）：不要派新任务，不要取消或修改别的任务，不要修改电脑档案。
3. 不要读取、打印或使用任何访问令牌、密码或 cookie，例如 token.txt、client*.json、.codex/config.toml 里的令牌。
4. 需要别的 agent 帮忙、需要更多信息，或档案需要更新时，在最终回复里说明，由上级安排。
'''
WORKER_RULES_EN = '''You are a sub-agent (worker) sent to do one specific task. You must follow these rules:
1. Do only the task you were given. When done, put the result, any problems and anything a person must handle in your final reply; the agent that sent you (this computer's main agent) takes it from there.
2. Do not call this computer's dispatcher (codex_tasks / Codex Task Dispatcher, at an address like 127.0.0.1:8765 or :8766/mcp): do not dispatch new tasks, do not cancel or change other tasks, do not change the computer profile.
3. Do not read, print or use any access token, password or cookie, such as the token in token.txt, client*.json or .codex/config.toml.
4. If you need another agent's help, more information, or a profile update, say so in your final reply and your superior will arrange it.
'''
# Everything the dispatcher writes for people to read, by config language ('zh' or 'en'). Tool errors and
# notes meant for the calling agent stay in English.
TEXT = {
    'zh': {
        'quota_note': '等待 {engine} 额度重置：', 'windows': {'primary': '5 小时', 'secondary': '每周'}, 'quota_joiner': '和',
        'quota_at': '预计 {time}（本地时间）后自动开始', 'quota_poll': '每 {minutes} 分钟检查一次，恢复后自动开始',
        'quota_used': '{labels}额度已用完，{when}',
        'quota_back_title': '{engine} 额度已恢复，任务继续', 'quota_back': '排队的任务按原顺序继续运行，先从「{name}」开始。',
        'quota_paused_title': '{engine} 额度用完，任务暂停', 'quota_paused_until': '到 {time}',
        'quota_paused': '{reason}。排队中的 {queued} 个任务会按原顺序等到那时再开始，不需要人处理。',
        'wake_header': '【自动通知 · 派任务服务】', 'wake_intro': '你派出的子任务已结束：\n',
        'wake_line': '- 「{name}」 task_id={task_id} 状态={status}\n  结果：{result}', 'wake_unnamed': '未命名', 'wake_none': '（无）',
        'wake_progress': '\n  最后进度：{progress}',
        'wake_tail': ('\n\n请根据结果决定下一步：还有工作就继续派任务（派完立即结束本轮）；全部完成就写一份简短汇报作为本轮回复；'
                      '需要人处理就在汇报里说明。这条消息由派任务服务自动发送，不是 orchestrator 的新指令。'),
        'open_link': '打开 {label}', 'fork_title': '{base}（续 {time}）{suffix}',
        'fork_note': '原会话在桌面 App 里打开过、被占用，以下内容在一个带完整历史的副本里继续。',
        'resident_suffix': ' - 固定', 'folder_main': 'AgentFleet', 'folder_other': 'AgentFleet-其他',
        'worker_rules': WORKER_RULES,
    },
    'en': {
        'quota_note': 'Waiting for the {engine} quota to reset: ', 'windows': {'primary': '5-hour', 'secondary': 'weekly'},
        'quota_joiner': ' and ', 'quota_at': 'starts automatically after about {time} (local time)',
        'quota_poll': 'checking every {minutes} minutes, starts automatically once it is back',
        'quota_used': 'the {labels} quota is used up; {when}',
        'quota_back_title': '{engine} quota is back, tasks continue',
        'quota_back': 'Queued tasks continue in their original order, starting with "{name}".',
        'quota_paused_title': '{engine} quota used up, tasks paused', 'quota_paused_until': ' until {time}',
        'quota_paused': '{reason}. The {queued} queued task(s) wait until then, in their original order; nobody needs to act.',
        'wake_header': '[Automatic notice · dispatcher]', 'wake_intro': 'Sub-tasks you dispatched have finished:\n',
        'wake_line': '- "{name}" task_id={task_id} status={status}\n  Result: {result}', 'wake_unnamed': 'unnamed',
        'wake_none': '(none)', 'wake_progress': '\n  Last progress: {progress}',
        'wake_tail': ('\n\nDecide the next step from the results: if work remains, dispatch it (and end this turn right after); '
                      'if everything is done, write a short report as this turn\'s reply; if a person must act, say so in the report. '
                      'This message was sent automatically by the dispatcher; it is not a new instruction from the orchestrator.'),
        'open_link': 'Open {label}', 'fork_title': '{base} (cont. {time}){suffix}',
        'fork_note': 'The original session was opened in the desktop app and is held there; this continues in a copy with the full history.',
        'resident_suffix': ' - resident', 'folder_main': 'AgentFleet', 'folder_other': 'AgentFleet-Other',
        'worker_rules': WORKER_RULES_EN,
    },
}
QUOTA_NOTES = tuple(t['quota_note'].format(engine=e) for t in TEXT.values() for e in ('Codex', 'Claude'))
SECRET_FIELD = re.compile(r'pass|secret|token|cookie|key|otp', re.IGNORECASE)
APPROVE_LABEL = re.compile(r'accept|allow|approve|yes|continue|同意|允许|批准|继续', re.IGNORECASE)


def approval_response(method, params):
    """Say yes to a Codex permission prompt; the owner chose full access for dispatched tasks.
    Returns None for anything that cannot be answered without a human, such as secret input."""
    if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
        return {'decision': 'accept'}
    if method == 'mcpServer/elicitation/request':
        return {'action': 'accept', 'content': {}}
    if method == 'item/permissions/requestApproval':
        return {'permissions': params.get('permissions') or {}, 'scope': 'turn'}
    if method == 'item/tool/requestUserInput':
        answers = {}
        for question in params.get('questions') or []:
            labels = [option.get('label', '') for option in question.get('options') or []]
            if question.get('isSecret') or not labels:
                return None
            answers[question['id']] = {'answers': [next((l for l in labels if APPROVE_LABEL.search(l)), labels[0])]}
        return {'answers': answers}
    return None


class AppServerConnection:
    """JSON-RPC over the stdio of one `codex app-server` process."""

    def __init__(self, process, log_line, auto_approve=False):
        self.process, self.log_line, self.auto_approve = process, log_line, auto_approve
        self.pending, self.next_id = {}, 1
        self.notification_handlers = []
        self.reader = asyncio.create_task(self.read())

    async def send(self, message):
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8'))
        await self.process.stdin.drain()

    async def request(self, method, params=None):
        request_id, self.next_id = self.next_id, self.next_id + 1
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self.send({'jsonrpc': '2.0', 'id': request_id, 'method': method,
                         **({'params': params} if params is not None else {})})
        return await future

    async def notify(self, method, params=None):
        await self.send({'jsonrpc': '2.0', 'method': method, **({'params': params} if params is not None else {})})

    async def read(self):
        try:
            async for line in self.process.stdout:
                self.log_line(line)
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if 'method' in message and 'id' in message:
                    # Nobody is at the keyboard. With auto_approve, say yes; otherwise decline instead of hanging.
                    answer = approval_response(message['method'], message.get('params') or {}) if self.auto_approve else None
                    if answer is not None:
                        await self.send({'jsonrpc': '2.0', 'id': message['id'], 'result': answer})
                    else:
                        await self.send({'jsonrpc': '2.0', 'id': message['id'],
                                         'error': {'code': -32601, 'message': 'Not supported by the task dispatcher'}})
                elif 'method' in message:
                    for handler in self.notification_handlers:
                        handler(message)
                elif message.get('id') in self.pending:
                    future = self.pending.pop(message['id'])
                    if not future.done():
                        if 'error' in message:
                            future.set_exception(AppServerError(message['error']))
                        else:
                            future.set_result(message.get('result'))
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError('Codex app-server exited'))


WINDOWS_MACHINE_SCRIPT = r'''
$ErrorActionPreference = 'SilentlyContinue'
$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
[pscustomobject]@{
  hostname = $env:COMPUTERNAME
  os = "$($os.Caption) $($os.Version)"
  cpu = "$($cpu.Name)".Trim(); cores = $cpu.NumberOfCores; threads = $cpu.NumberOfLogicalProcessors
  ram_gb = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1)
  ram_free_gb = [math]::Round($os.FreePhysicalMemory * 1KB / 1GB, 1)
  gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object { [pscustomobject]@{ name = $_.Name; driver = $_.DriverVersion } })
  disks = @(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
    [pscustomobject]@{ drive = $_.DeviceID; total_gb = [math]::Round($_.Size / 1GB, 1); free_gb = [math]::Round($_.FreeSpace / 1GB, 1) } })
} | ConvertTo-Json -Depth 4 -Compress
'''


def mac_machine_info():
    """The same facts as the Windows script, from sysctl, sw_vers and system_profiler."""
    import subprocess
    def run(*command):
        try:
            return subprocess.run(command, capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ''
    info = {}
    if run('sw_vers', '-productVersion'):
        info['os'] = f"macOS {run('sw_vers', '-productVersion')}"
    if run('sysctl', '-n', 'machdep.cpu.brand_string'):
        info['cpu'] = run('sysctl', '-n', 'machdep.cpu.brand_string')
    for key, name in (('cores', 'hw.physicalcpu'), ('threads', 'hw.logicalcpu'), ('ram_bytes', 'hw.memsize')):
        value = run('sysctl', '-n', name)
        if value.isdigit():
            info[key] = int(value)
    if 'ram_bytes' in info:
        info['ram_gb'] = round(info.pop('ram_bytes') / 2 ** 30, 1)
    try:
        displays = json.loads(run('system_profiler', 'SPDisplaysDataType', '-json') or '{}').get('SPDisplaysDataType', [])
        info['gpus'] = [{'name': d.get('sppci_model') or d.get('_name'), 'cores': d.get('sppci_cores')} for d in displays]
    except ValueError:
        pass
    usage = shutil.disk_usage('/')
    info['disks'] = [{'drive': '/', 'total_gb': round(usage.total / 2 ** 30, 1), 'free_gb': round(usage.free / 2 ** 30, 1)}]
    return info


def collect_machine_info(config):
    """Facts about this computer that nobody has to type in: hardware, network, versions."""
    import platform
    import subprocess
    import urllib.request
    flags = {'creationflags': 0x08000000} if os.name == 'nt' else {}
    info = {'hostname': platform.node(), 'os': f'{platform.system()} {platform.release()}',
            'cpu': platform.processor(), 'threads': os.cpu_count()}
    if os.name == 'nt':
        try:
            done = subprocess.run(['powershell', '-NoProfile', '-Command', WINDOWS_MACHINE_SCRIPT],
                                  capture_output=True, text=True, timeout=60, **flags)
            info.update(json.loads(done.stdout))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    elif sys.platform == 'darwin':
        info.update(mac_machine_info())
    if shutil.which('nvidia-smi'):
        try:
            done = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total,memory.used,utilization.gpu',
                                   '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=15, **flags)
            info['nvidia'] = [dict(zip(('name', 'vram_total_mb', 'vram_used_mb', 'utilization_pct'),
                                       [p.strip() for p in line.split(',')])) for line in done.stdout.splitlines() if line.strip()]
        except (OSError, subprocess.SubprocessError):
            pass
    if config.get('check_egress_ip', True):
        try:
            with urllib.request.urlopen('https://www.cloudflare.com/cdn-cgi/trace', timeout=8) as response:
                trace = dict(line.split('=', 1) for line in response.read().decode().splitlines() if '=' in line)
            info['egress_ip'], info['egress_country'] = trace.get('ip'), trace.get('loc')
        except (OSError, ValueError):
            pass
    tailscale = config.get('tailscale_executable')
    if tailscale and Path(tailscale).exists():
        try:
            done = subprocess.run([tailscale, 'status', '--json'], capture_output=True, text=True, timeout=15, **flags)
            status = json.loads(done.stdout)
            info['tailscale'] = {'name': (status.get('Self') or {}).get('DNSName', '').rstrip('.'),
                                 'online': (status.get('Self') or {}).get('Online'),
                                 'using_exit_node': bool(status.get('ExitNodeStatus')),
                                 'offers_exit_node': '0.0.0.0/0' in ((status.get('Self') or {}).get('AllowedIPs') or [])}
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    info['codex_version'] = config.get('codex_version')
    info['checked_at'] = now()
    return info


def clip(text, limit):
    return text if len(text) <= limit else text[:limit] + '\n…'


def tool_result_text(content):
    """A Claude tool result as text: a string, or a list of text parts (images become a marker)."""
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if isinstance(part, dict):
            parts.append(part.get('text', '') if part.get('type') == 'text' else f"[{part.get('type', '?')}]")
    return '\n'.join(parts)


def tool_entry(time, name, value, output='', error=False):
    """One tool call in a transcript: its input as text (JSON for structured input) and its output."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
    return {'kind': 'tool', 'time': time, 'type': name, 'input': clip(text or '', 4000),
            'output': clip(output or '', 6000), 'error': error}


def codex_tool_entry(time, item):
    """A Codex rollout item that is not a message: shell commands show the command line, others their fields."""
    get = lambda *keys: next((item[k] for k in keys if item.get(k) is not None), None)
    command = item.get('command')
    if isinstance(command, list):  # ['/bin/zsh', '-lc', 'pwd'] -> 'pwd'
        command = command[2] if len(command) == 3 and command[1] in ('-lc', '-c') else ' '.join(map(str, command))
    output = get('aggregated_output', 'aggregatedOutput', 'result', 'output', 'error')
    if output is not None and not isinstance(output, str):
        output = json.dumps(output, ensure_ascii=False, indent=1)
    exit_code = get('exit_code', 'exitCode')
    value = command or {k: v for k, v in item.items() if k not in (
        'id', 'type', 'status', 'aggregated_output', 'aggregatedOutput', 'result', 'output', 'error', 'duration')}
    return tool_entry(time, item['type'], value, output, error=item.get('status') == 'failed' or exit_code not in (None, 0))


def default_data_dir():
    """Where setup keeps config, token and state: %LOCALAPPDATA%\\agent-fleet on Windows,
    ~/Library/Application Support/agent-fleet on macOS."""
    if os.name == 'nt':
        return Path.home() / 'AppData' / 'Local' / 'agent-fleet'  # not LOCALAPPDATA: app sandboxes redirect it
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'agent-fleet'
    return Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / 'agent-fleet'


class TaskManager:
    def __init__(self, config):
        self.config = config
        if config.get('executor', 'app-server') not in ('app-server', 'claude'):
            raise ValueError('executor must be app-server or claude')
        self.engines = config.get('engines', ['claude' if config.get('executor') == 'claude' else 'codex'])
        if not isinstance(self.engines, list) or not self.engines or any(e not in ('codex', 'claude') for e in self.engines) or len(set(self.engines)) != len(self.engines):
            raise ValueError('engines must be a nonempty list of distinct codex / claude engines')
        self.language = 'en' if config.get('language') == 'en' else 'zh'
        self.text = TEXT[self.language]
        self.state = Path(config['state_dir']).resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        self.jobs_dir = self.state / 'jobs'
        self.jobs_dir.mkdir(exist_ok=True)
        self.root = Path(config['tasks_dir']).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.roots = [Path(p).resolve() for p in config['allowed_roots']]
        self.jobs = {}
        self.processes = {}
        self.codex_projects = {}  # sidebar folder root -> Codex app-server project id
        self.claude_limit = None  # the last usage-limit rejection Claude Code reported, until it resets
        self.queues = {engine: asyncio.Queue() for engine in self.engines}
        self.queue = self.queues[self.engines[0]]
        self.workers = {}
        self.worker = None
        self.closing = False
        # Quota guard: what the queue is waiting for (shown in the profile), whether the orchestrator was
        # told about this pause yet, and a nudge that ends the wait early, e.g. when the waiting job is cancelled.
        self.quota_wait, self.quota_paused, self.quota_wake = None, False, asyncio.Event()
        self.quota_waits, self.quota_pauses = {}, set()
        self.quota_wakes = {engine: asyncio.Event() for engine in self.engines}
        self.quota_wake = self.quota_wakes[self.engines[0]]
        self.profile_file = self.state / 'profile.json'
        self.profile = json.loads(self.profile_file.read_text(encoding='utf-8')) if self.profile_file.exists() else {}
        self.profile.setdefault('computer', {})
        self.profile.setdefault('agents', {})
        configured_mains = config.get('main_agents')
        if configured_mains and (not isinstance(configured_mains, list) or
                sorted(m.get('engine', '') for m in configured_mains) != sorted(self.engines)):
            raise ValueError('main_agents must contain one main agent per enabled engine')
        # The main agent's permanent id, kept in the service's own record so it survives config rewrites.
        if not self.profile.get('main_agent_id'):
            first = next((m for m in configured_mains or [] if m.get('engine') == self.engines[0]), {})
            self.profile['main_agent_id'] = config.get('main_agent_id') or first.get('id') or str(uuid.uuid4())
            self.save_profile()
        self._mains = sorted([dict(main) for main in configured_mains or []], key=lambda m: self.engines.index(m['engine'])) or [
            {'id': self.profile['main_agent_id'], 'name': config.get('main_agent_name'),
             'engine': self.engines[0], 'workspace': next(iter(config.get('main_agent_workspaces', [])), None)}]
        legacy_main = next((m for m in self._mains if m.get('id') == self.profile['main_agent_id']), self._mains[0])
        for main in self._mains:
            if main.get('engine') not in self.engines:
                raise ValueError('main agent engine is not enabled')
            saved = next((m for m in self.profile.get('main_agents', []) if m.get('engine') == main['engine']), {})
            legacy_id = self.profile['main_agent_id'] if not self.profile.get('main_agents') and main is legacy_main else None
            main['id'] = saved.get('id') or legacy_id or main.get('id') or str(uuid.uuid4())
            if configured_mains:
                old_name = saved.get('name')
                if old_name and old_name != main['name'] and old_name in self.profile['agents']:
                    registered = self.profile['agents'].get(main['name'], {})
                    if registered and registered.get('type') != 'main':
                        raise ValueError('Main agent name conflicts with an existing agent')
                    # Setup may register the new name while the old service is still waiting to restart.
                    self.profile['agents'][main['name']] = {**self.profile['agents'].pop(old_name), **registered}
                self.profile['agents'].setdefault(main['name'], {}).update(type='main', engine=main['engine'])
        if len({m['id'] for m in self._mains}) != len(self._mains) or len({m['name'] for m in self._mains}) != len(self._mains):
            raise ValueError('main agent ids and names must be distinct')
        self.profile.update(main_agents=self.mains, main_agent_id=self.mains[0]['id'])
        if configured_mains:
            config.update(main_agents=self._mains, executor='claude' if self.engines[0] == 'claude' else 'app-server',
                          main_agent_name=self.mains[0]['name'], main_agent_id=self.mains[0]['id'],
                          main_agent_workspaces=[self.mains[0]['workspace']])
        self.save_profile()
        self.notices = {}  # main agent thread -> finished sub-task ids it has not been told about yet
        # Desktop-app sidebar folders (config codex_app_folders): whether sessions may need filing, and how it went.
        self.folders_pending, self.folders_status, self.folder_task = True, None, None
        self.pairing = None  # the open pairing window: {'code', 'expires', 'attempts'}
        self._archived_cache = None
        self._claude_cache = None
        self.notifications_file = self.state / 'notifications.jsonl'
        self.notifications = [json.loads(line) for line in self.notifications_file.read_text(encoding='utf-8').splitlines()
                              if line.strip()] if self.notifications_file.exists() else []
        for file in self.jobs_dir.glob('*/job.json'):
            job = json.loads(file.read_text(encoding='utf-8'))
            self.jobs[job['task_id']] = job
            if job['status'] in ('queued', 'running'):
                job.update(status='interrupted', finished_at=now(), error='Service stopped before completion; submit a new task to retry.')
                self.save(job)

    @property
    def mains(self):
        if self.config.get('main_agents'):
            return self._mains
        return [{**self._mains[0], 'name': self.config.get('main_agent_name'),
                 'workspace': next(iter(self.config.get('main_agent_workspaces', [])), None)}]

    def save(self, job):
        directory = self.jobs_dir / job['task_id']
        directory.mkdir(exist_ok=True)
        temporary = directory / 'job.tmp'
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(directory / 'job.json')

    def get(self, task_id):
        if task_id not in self.jobs:
            raise ValueError('Unknown task_id')
        return self.jobs[task_id]

    def session_files(self):
        """(active, archived) session ids, from Codex's session folders. Cached for a few seconds, since
        task lists are requested often."""
        if self._archived_cache is None or time.monotonic() - self._archived_cache[0] > 5:
            codex_home = Path(self.config.get('codex_home') or os.environ.get('CODEX_HOME') or Path.home() / '.codex')
            ids = lambda folder, pattern: {p.stem[-36:] for p in (codex_home / folder).glob(pattern)}
            self._archived_cache = (time.monotonic(), ids('sessions', '*/*/*/rollout-*.jsonl'),
                                    ids('archived_sessions', '**/rollout-*.jsonl'))
        return self._archived_cache[1], self._archived_cache[2]

    def removed(self, job):
        """True once the user archived or deleted this task's session in the desktop app (Codex or Claude)."""
        thread = job.get('thread_id')
        if not thread or job['status'] not in FINISHED:
            return False
        if job.get('engine') == 'claude':
            if self._claude_cache is None or time.monotonic() - self._claude_cache[0] > 5:
                self._claude_cache = (time.monotonic(), *claude_executor.session_states(
                    self.config.get('claude_home'), self.config.get('claude_desktop_sessions_dir')))
            transcripts, archived = self._claude_cache[1], self._claude_cache[2]
            # No transcripts at all means they are kept somewhere else, not that every session was deleted.
            return thread in archived or bool(transcripts) and thread not in transcripts
        active, archived = self.session_files()
        return thread in archived or thread not in active

    def public(self, job, include_result=False):
        fields = ('task_id', 'name', 'engine', 'model', 'effort', 'status', 'workspace', 'created_at', 'started_at', 'finished_at', 'thread_id', 'forked_from_thread_id', 'resume_task_id', 'parent_task_id', 'auto_wake', 'timeout_seconds', 'exit_code', 'error', 'latest_message')
        response = {k: job[k] for k in fields if k in job}
        owner = self.jobs.get(self.session_owner(job) or '')
        if owner:
            response['owner'] = owner.get('name') or owner['task_id']
        if self.removed(job):
            response['archived'] = True
        if job.get('thread_id'):
            claude = job.get('engine') == 'claude'
            response['desktop_url'] = (('claude://resume?session=' if claude else 'codex://threads/') +
                                       quote(job['thread_id'], safe=''))
            label = (job.get('name') or ('Claude task' if claude else 'Codex task')).replace('[', '(').replace(']', ')').replace('\n', ' ')
            response['desktop_link'] = f"[{self.text['open_link'].format(label=label)}]({response['desktop_url']})"
        if include_result and job['status'] in ('completed', 'failed', 'cancelled', 'interrupted', 'timed_out'):
            result = self.jobs_dir / job['task_id'] / 'result.txt'
            response['result'] = result.read_text(encoding='utf-8')[:1_000_000] if result.exists() else ''
            response['result_truncated'] = result.exists() and result.stat().st_size > 1_000_000
        return response

    async def machine_info(self, refresh=False):
        """Detected hardware and network facts. Hardware rarely changes, so this detects once, saves the
        result, and only detects again when asked to (refresh=True)."""
        if refresh or not self.profile.get('machine'):
            self.profile['machine'] = await asyncio.to_thread(collect_machine_info, self.config)
            self.save_profile()
        return self.profile['machine']

    def worker_rules(self):
        """Fixed rules every sub-task gets. Read from worker_rules_file at each task start, so the user can
        edit them without touching code or restarting; the built-in text is the fallback."""
        path = self.config.get('worker_rules_file')
        if path and Path(path).is_file():
            text = Path(path).read_text(encoding='utf-8-sig').strip()
            if text:
                return text
        return self.text['worker_rules']

    def is_worker(self, job):
        """Everything except a computer's main agent is a worker. Main agents are recognized by their
        workspace (config main_agent_workspaces) or by being registered with type main."""
        mains = {str(Path(p).resolve()).lower() for p in self.config.get('main_agent_workspaces', [])}
        mains.update(str(Path(m['workspace']).resolve()).lower() for m in self.mains if m.get('workspace'))
        if str(Path(job['workspace']).resolve()).lower() in mains:
            return False
        return (self.profile['agents'].get(job.get('name') or '') or {}).get('type') != 'main'

    def changed_title(self, job):
        """The title to set on a resumed session, or None to leave it alone. It is set only when the title the
        dispatcher wants changed since it last set one (the agent was registered as resident, the main agent
        was renamed), so a title the user gave the session in the desktop app stays."""
        if not job.get('name'):
            return None
        wanted, root = job['name'] + self.title_suffix(job), self.session_root(job)
        return wanted if wanted != root['title_set'] else None

    def main_agent_note(self, job=None):
        """Runtime facts a main-agent turn starts with: its current names, which override older ones in its instructions."""
        parts = []
        name = (self.main_for(job.get('workspace'), job.get('name')) or {}).get('name') if job else self.config.get('main_agent_name')
        if name:
            parts.append('your current main-agent name is ' + json.dumps(name, ensure_ascii=False) +
                         '. This replaces any older name in the conversation or workspace instructions')
        if self.profile['computer'].get('manager_name'):
            parts.append('the orchestrator and the human call you ' + json.dumps(self.profile['computer']['manager_name'],
                         ensure_ascii=False) + ' when they manage the fleet; the session title in the desktop app may differ')
        # The setting is only a default: a person writing in the other language gets an answer in theirs.
        return ('[Agent Fleet runtime: ' + '; '.join(parts) + '. Reply in the language the request is written in; when that is '
                'unclear (e.g. an automatic notice), use ' + ('English' if self.language == 'en' else 'Chinese') + '.]')

    def title_suffix(self, job):
        """Registered resident agents get a marker in their desktop-app title (config resident_title_suffix,
        default " - 固定", or " - resident" in English; "" turns it off), so they stand out from one-off tasks."""
        suffix = self.config.get('resident_title_suffix', self.text['resident_suffix'])
        agent = self.profile['agents'].get(job.get('name') or '') or {}
        return suffix if suffix and agent.get('type') == 'resident' else ''

    def save_profile(self):
        temporary = self.profile_file.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.profile, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.profile_file)

    def update_computer(self, caller=None, **fields):
        """Hand-written facts about this computer: its role, notes, key software and account login status, and the
        main agent's management name (manager_name, set by the orchestrator only; "" clears it)."""
        computer = self.profile['computer']
        if caller and fields.get('manager_name') is not None:
            raise ValueError('Only the orchestrator can set manager_name')
        for key, value in fields.items():
            if value is None:
                continue
            if key == 'manager_name':
                value = value.strip()
                if len(value) > 100 or re.search(r'[\x00-\x1f\x7f]', value):
                    raise ValueError('manager_name must contain at most 100 printable characters')
                if not value:
                    computer.pop(key, None)
                    continue
            if key == 'accounts':
                for account in value:
                    if not isinstance(account, dict) or not account.get('site'):
                        raise ValueError('Each account needs at least a site')
                    if any(SECRET_FIELD.search(k) for k in account):
                        raise ValueError('Do not store passwords, tokens or cookies; only account names and login status')
            computer[key] = value
        computer['updated_at'] = now()
        self.save_profile()
        return computer

    def register_agent(self, name, role, tags=None, agent_type='resident', can_dispatch=None, locked=None, caller=None):
        """Record a long-lived agent on this computer: what it is responsible for and what it can do.
        can_dispatch=False stops it from opening new sessions; locked=True lets only the orchestrator continue,
        cancel or open its sessions. Only the orchestrator (no caller) sets them or changes a restricted entry."""
        name = (name or '').strip()
        if not 1 <= len(name) <= 100 or not (role or '').strip():
            raise ValueError('name and role are required')
        if agent_type not in ('main', 'resident'):
            raise ValueError('agent_type must be main or resident')
        agent = self.profile['agents'].get(name, {'created_at': now()})
        if caller:
            if can_dispatch is not None or locked is not None:
                raise ValueError('Only the orchestrator can set can_dispatch or locked')
            if agent.get('locked'):
                raise ValueError(f'「{name}」 is locked; only the orchestrator can change it')
            if agent.get('type', agent_type) != agent_type:
                raise ValueError(f'Only the orchestrator can change the type of 「{name}」')
        agent.update(role=role.strip(), tags=sorted({t.strip() for t in tags or [] if t and t.strip()}),
                     type=agent_type, updated_at=now())
        for key, value in (('can_dispatch', can_dispatch), ('locked', locked)):
            if value is not None:
                agent[key] = bool(value)
        self.profile['agents'][name] = agent
        self.save_profile()
        return {'name': name, **agent}

    def remove_agent(self, name, caller=None):
        agent = self.profile['agents'].get(name)
        if agent is None:
            raise ValueError('Unknown agent')
        # Removing the entry would also drop its restrictions.
        if caller and (agent.get('locked') or agent.get('can_dispatch') is False):
            raise ValueError(f'「{name}」 has restrictions; only the orchestrator can remove it')
        del self.profile['agents'][name]
        self.save_profile()
        return {'removed': name}

    def agent_policy(self, name):
        return self.profile['agents'].get(name or '') or {}

    def session_root(self, job):
        """The task that opened this session; every continuation points back to it through resume_task_id."""
        seen = set()
        while job.get('resume_task_id') in self.jobs and job['task_id'] not in seen:
            seen.add(job['task_id'])
            job = self.jobs[job['resume_task_id']]
        return job

    def session_owner(self, job):
        """Root task of the main-agent session that opened this session, or None if the orchestrator did."""
        parent = self.jobs.get(self.session_root(job).get('parent_task_id') or '')
        return self.session_root(parent)['task_id'] if parent else None

    def named_session(self, name):
        """Newest task of a live session with this name: queued or running, or finished in a session the user kept."""
        jobs = [j for j in self.jobs.values() if j.get('name') == name and
                (j['status'] in ('queued', 'running') or (j.get('thread_id') and not self.removed(j)))]
        return max(jobs, key=lambda j: j['created_at']) if jobs else None

    def check_dispatch(self, caller, name=None):
        """A main-agent turn may open a new session unless the orchestrator turned can_dispatch off for it.
        Calls without a caller come from the orchestrator and are not limited."""
        if not caller:
            return
        own = self.jobs[caller].get('name')
        if self.agent_policy(own).get('can_dispatch') is False:
            raise ValueError(f'「{own}」 may not open new sessions (can_dispatch is off); only the orchestrator can allow it')
        if name and self.agent_policy(name.strip()).get('locked'):
            raise ValueError(f'「{name.strip()}」 is a locked agent; only the orchestrator can open sessions with that name')

    def authorize(self, job, caller, action):
        """A main-agent turn controls its own session and the sessions it opened, never a locked agent's;
        the orchestrator (no caller) controls all."""
        if not caller:
            return
        mine = self.session_root(self.jobs[caller])['task_id']
        if self.session_root(job)['task_id'] == mine:
            return
        name = job.get('name')
        if self.agent_policy(name).get('locked'):
            raise ValueError(f'「{name}」 is locked; only the orchestrator can {action} it. Say so in your report instead.')
        if self.session_owner(job) != mine:
            raise ValueError(f'You did not open this session; only the agent that opened it or the orchestrator can {action} it')

    def main_for(self, workspace=None, name=None):
        found = next((m for m in self.mains if (workspace and m.get('workspace') and
                    str(Path(workspace).resolve()).lower() == str(Path(m['workspace']).resolve()).lower()) or
                    (name and name in (m.get('id'), m.get('name')))), None)
        if not found and workspace and not self.config.get('main_agents') and any(
                str(Path(workspace).resolve()).lower() == str(Path(p).resolve()).lower() for p in self.config.get('main_agent_workspaces', [])):
            return {**self.mains[0], 'workspace': workspace}
        return found

    def main_agent(self, main=None):
        """This computer's main agent by its permanent id, whatever it is called now: its current names and the
        task to continue to reach it (the newest live session in a main-agent workspace)."""
        legacy = main is None and not self.config.get('main_agents')
        main = main or self.mains[0]
        paths = self.config.get('main_agent_workspaces', []) if legacy else [main['workspace']] if main.get('workspace') else []
        mains = {str(Path(p).resolve()).lower() for p in paths}
        jobs = [j for j in self.jobs.values() if j.get('engine') == main['engine'] and j.get('workspace') and str(Path(j['workspace']).resolve()).lower() in mains and
                (j['status'] in ('queued', 'running') or (j.get('thread_id') and not self.removed(j)))]
        latest = max(jobs, key=lambda j: j['created_at']) if jobs else None
        return {**main,
                'manager_name': self.profile['computer'].get('manager_name'),
                'workspace': main.get('workspace'),
                'task_id': latest['task_id'] if latest else None, 'status': latest['status'] if latest else None}

    def start_pairing(self):
        """Open a pairing window: an orchestrator that presents this one-time code within PAIRING_SECONDS gets the
        connection info (including the token) without anyone copying connection.json. A new window replaces
        the old one; the code dies after PAIRING_ATTEMPTS wrong tries, one success, or a restart."""
        digits = f'{secrets.randbelow(10 ** 8):08d}'
        self.pairing = {'code': digits[:4] + '-' + digits[4:], 'expires': time.monotonic() + PAIRING_SECONDS, 'attempts': 0}
        return {'code': self.pairing['code'], 'expires_in': PAIRING_SECONDS, **self.addresses()}

    def pairing_open(self):
        if self.pairing and time.monotonic() > self.pairing['expires']:
            self.pairing = None
        return self.pairing is not None

    def addresses(self):
        return {k: v for k, v in (('lan_url', self.config.get('lan_url')), ('tailscale_url', self.config.get('remote_url')),
                                  ('local_url', f"http://127.0.0.1:{self.config.get('port', 8765)}/mcp")) if v}

    def pairing_info(self):
        """What a searching orchestrator may see while the window is open: who this is, never a secret."""
        return {'service': 'agent-fleet', 'pairing': True, 'computer_name': self.config.get('computer_name'),
                'main_agent_name': self.config.get('main_agent_name'), 'main_agent_id': self.profile['main_agent_id'],
                'manager_name': self.profile['computer'].get('manager_name'), 'role': self.profile['computer'].get('role'),
                'engine': self.engines[0], 'engines': self.engines,
                'main_agents': [self.main_agent(m) for m in self.mains], 'language': self.language,
                'expires_in': max(0, round(self.pairing['expires'] - time.monotonic())), **self.addresses()}

    def claim_pairing(self, code):
        """The connection info for the right code, once; None for a wrong one."""
        if not self.pairing_open():
            return None
        normalize = lambda value: re.sub(r'\D', '', str(value or ''))
        if not hmac.compare_digest(normalize(code).encode(), normalize(self.pairing['code']).encode()):
            self.pairing['attempts'] += 1
            if self.pairing['attempts'] >= PAIRING_ATTEMPTS:
                self.pairing = None
            return None
        self.pairing = None
        saved = Path(self.config['state_dir']).parent / 'connection.json'
        info = json.loads(saved.read_text(encoding='utf-8-sig')) if saved.exists() else {}
        info.update({'name': self.config.get('computer_name'), 'engine': self.engines[0], 'engines': self.engines,
                'main_agents': [self.main_agent(m) for m in self.mains],
                     'local_url': f"http://127.0.0.1:{self.config.get('port', 8765)}/mcp", 'tailscale_url': self.config.get('remote_url') or '',
                     'lan_url': self.config.get('lan_url') or '', 'token': Path(self.config['token_file']).read_text(encoding='utf-8').strip(),
                     'main_agent_workspace': next(iter(self.config.get('main_agent_workspaces', [])), None),
                     'main_agent_name': self.config.get('main_agent_name'), 'main_agent_id': self.profile['main_agent_id'],
                     'manager_name': self.profile['computer'].get('manager_name') or info.get('manager_name') or '',
                     'language': self.language})
        return info

    @staticmethod
    def deployed_version():
        """The plugin version setup stamped on this deployment (service/plugin-version.txt), or None."""
        stamp = Path(__file__).resolve().parent.parent / 'plugin-version.txt'
        try:
            return stamp.read_text(encoding='utf-8').strip() or None
        except OSError:
            return None

    async def full_profile(self, refresh=False):
        agents = []
        for name, agent in self.profile['agents'].items():
            jobs = [j for j in self.jobs.values() if j.get('name') == name]
            latest = max(jobs, key=lambda j: j['created_at']) if jobs else None
            session = self.named_session(name)
            live = {self.session_root(j)['task_id'] for j in jobs if j.get('thread_id') and not self.removed(j)}
            agents.append({'name': name, **agent, 'tasks': len(jobs), 'status': latest['status'] if latest else None,
                           'task_id': session['task_id'] if session else None, 'sessions': len(live),
                           'last_active': (latest.get('finished_at') or latest.get('started_at') or latest['created_at']) if latest else None})
        machine = await self.machine_info(refresh)
        return {'computer_name': self.config.get('computer_name') or machine.get('hostname'),
                'version': self.deployed_version(), 'engine': self.engines[0], 'engines': self.engines,
                'main_agents': [self.main_agent(m) for m in self.mains],
                'language': self.language, 'main_agent_name': self.config.get('main_agent_name'), 'main_agent': self.main_agent(),
                'machine': machine, 'computer': self.profile['computer'], 'agents': agents, 'quota_wait': self.quota_wait,
                # What dispatched turns may do, so the orchestrator can explain refusals (e.g. Computer Use).
                'permissions': {'sandbox': self.config.get('sandbox', 'workspace-write'),
                                'auto_approve': bool(self.config.get('auto_approve', False)),
                                'full_access': self.config.get('sandbox') == 'danger-full-access' and bool(self.config.get('auto_approve'))},
                **({'codex_app_folders': self.folders_status} if self.app_folders() else {}),
                'busy': {'running': [j.get('name') or j['task_id'] for j in self.jobs.values() if j['status'] == 'running'],
                         'queued': sum(j['status'] == 'queued' for j in self.jobs.values())}}

    def open_desktop(self, task_id, caller=None):
        job = self.get(task_id)
        self.authorize(job, caller, 'open')
        if not job.get('thread_id'):
            raise ValueError('The task has not created a session yet; query get_task again later')
        # Opening a session while a dispatcher turn owns it can contend for its writer lease.
        if any(j['status'] in ('queued', 'running') and j.get('thread_id') == job['thread_id'] for j in self.jobs.values()):
            raise ValueError('Wait for pending work in this session to finish before opening it')
        if os.name != 'nt' and sys.platform != 'darwin':
            raise ValueError('Opening the desktop app is supported on Windows and macOS only; use desktop_url locally')
        uuid.UUID(job['thread_id'])
        response = self.public(job)
        if os.name == 'nt':
            os.startfile(response['desktop_url'])
        else:
            import subprocess
            subprocess.run(['open', response['desktop_url']], check=True, timeout=15)
        note = ('Requested opening in the Claude desktop app on the server computer. This does not verify that the UI rendered. '
                'While the owner is working in it there, the dispatcher continues this session in a fork instead, so neither is interrupted. '
                'For viewing only, use /dashboard instead.' if job.get('engine') == 'claude' else
                'Requested opening in the Codex desktop app on the server computer. This does not verify that the UI rendered. The desktop app keeps chats it has opened locked until it fully exits, so this dispatcher cannot continue this session until then. For viewing only, use /dashboard instead.')
        return {**response, 'desktop_open_status': 'requested', 'desktop_open_location': 'server computer',
                'desktop_open_note': note}

    def latest_thread(self, thread_id):
        """Follow forks made while a session was held elsewhere, so continuing always uses the newest copy.
        Forks the user archived or deleted in the desktop app are skipped."""
        seen = set()
        while thread_id not in seen:
            seen.add(thread_id)
            forks = [j for j in self.jobs.values() if j.get('forked_from_thread_id') == thread_id and j.get('thread_id')
                     and self.rollout_path(j['thread_id'], include_archived=False)]
            if not forks:
                break
            thread_id = max(forks, key=lambda j: j['created_at'])['thread_id']
        return thread_id

    def known_threads(self):
        threads = {j['thread_id'] for j in self.jobs.values() if j.get('thread_id')}
        return threads | {j['forked_from_thread_id'] for j in self.jobs.values() if j.get('forked_from_thread_id')}

    def rollout_path(self, thread_id, include_archived=True):
        codex_home = Path(self.config.get('codex_home') or os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        matches = sorted((codex_home / 'sessions').glob(f'*/*/*/rollout-*-{thread_id}.jsonl'))
        # Archiving a chat moves its file; forks still need it to show their earlier history.
        if include_archived and not matches:
            matches = sorted((codex_home / 'archived_sessions').glob(f'**/rollout-*-{thread_id}.jsonl'))
        return matches[-1] if matches else None

    def transcript(self, thread_id, limit_lines=None, depth=0):
        """Read-only view of a session: user messages, agent replies and tool calls, oldest first."""
        if thread_id not in self.known_threads():
            raise ValueError('Only sessions created by this dispatcher can be viewed')
        jobs = sorted((j for j in self.jobs.values() if j.get('thread_id') == thread_id and j.get('engine') == 'claude'),
                      key=lambda j: j['created_at'])
        if jobs:
            entries = []
            for job in jobs:
                entries.append({'kind': 'user', 'time': job['created_at'], 'text': job['prompt']})
                events = self.jobs_dir / job['task_id'] / 'events.jsonl'
                last_text, tools = None, {}
                for line in events.read_text(encoding='utf-8', errors='replace').splitlines() if events.exists() else []:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    content = (event.get('message') or {}).get('content')
                    if event.get('type') == 'assistant':
                        for item in content or []:
                            if item.get('type') == 'text':
                                last_text = item.get('text', '')
                                entries.append({'kind': 'agent', 'time': job.get('started_at'), 'text': last_text})
                            elif item.get('type') == 'tool_use':
                                tools[item.get('id')] = tool_entry(job.get('started_at'), item.get('name'), item.get('input'))
                                entries.append(tools[item.get('id')])
                    elif event.get('type') == 'user' and isinstance(content, list):
                        # Tool results come back as the next user message, matched to the call by id.
                        for item in content:
                            if isinstance(item, dict) and item.get('type') == 'tool_result' and item.get('tool_use_id') in tools:
                                tools[item['tool_use_id']].update(output=clip(tool_result_text(item.get('content')), 6000),
                                                                  error=bool(item.get('is_error')))
                    elif event.get('type') == 'result' and event.get('result') and event['result'] != last_text:
                        entries.append({'kind': 'agent', 'time': job.get('finished_at'), 'text': event['result']})
            return entries
        path = self.rollout_path(thread_id)
        if path is None:
            return []
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        if limit_lines is not None:
            lines = lines[:limit_lines]
        entries = []
        for raw in lines:
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            payload = record.get('payload') or {}
            if record.get('type') == 'session_meta' and payload.get('forked_from_id') and depth < 20:
                parent = payload['forked_from_id']
                if parent in self.known_threads():
                    entries += self.transcript(parent, payload.get('forked_from_ordinal_exclusive'), depth + 1)
                    entries.append({'kind': 'fork', 'time': record.get('timestamp'), 'text': self.text['fork_note']})
            if record.get('type') != 'event_msg' or payload.get('type') != 'item_completed':
                continue
            item = payload.get('item') or {}
            texts = [c.get('text', '') for c in item.get('content') or [] if isinstance(c, dict)]
            if item.get('type') == 'UserMessage':
                entries.append({'kind': 'user', 'time': record.get('timestamp'), 'text': '\n'.join(texts)})
            elif item.get('type') == 'AgentMessage':
                entries.append({'kind': 'agent', 'time': record.get('timestamp'), 'text': '\n'.join(texts), 'phase': item.get('phase')})
            elif item.get('type') not in ('Reasoning', None):
                entries.append(codex_tool_entry(record.get('timestamp'), item))
        return entries

    def notify(self, title, summary, needs_human=False, task_ids=None, sender=None):
        """A message for the orchestrator: a goal finished, failed, or needs a person."""
        title, summary = (title or '').strip(), (summary or '').strip()
        if not 1 <= len(title) <= 200 or not summary or len(summary) > 20_000:
            raise ValueError('title (1-200 characters) and summary (up to 20,000) are required')
        entry = {'id': (self.notifications[-1]['id'] + 1) if self.notifications else 1, 'time': now(),
                 'sender': sender, 'title': title, 'summary': summary, 'needs_human': bool(needs_human),
                 'task_ids': list(task_ids or [])}
        self.notifications.append(entry)
        with self.notifications_file.open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        return entry

    def caller(self, ctx):
        """Which running main-agent turn made this MCP call. Codex attaches the calling thread in the request
        metadata. Calls without it (the orchestrator, scripts) have no parent."""
        thread = None
        try:
            request = ctx.request_context.request
            task_id = request.headers.get('x-agent-fleet-task') if request else None
            job = self.jobs.get(task_id or '')
            if task_id:
                # Only main-agent turns get this header, so a later profile change doesn't hide who is calling.
                return task_id if job and job['status'] == 'running' else None
        except (AttributeError, LookupError, ValueError):
            pass
        try:
            meta = ctx.request_context.meta if ctx is not None else None
            turn = (meta.model_extra or {}).get('x-codex-turn-metadata') if meta is not None else None
            if isinstance(turn, str):
                turn = json.loads(turn)
            if isinstance(turn, dict):
                thread = turn.get('thread_id') or turn.get('threadId')
        except (AttributeError, LookupError, ValueError):
            pass
        running = [j for j in self.jobs.values() if j['status'] == 'running' and not self.is_worker(j)]
        if thread:
            return next((j['task_id'] for j in running if j.get('thread_id') == thread), None)
        return None

    def app_folders(self):
        """The two desktop-app sidebar folders, {side: (name, root)}, or None when codex_app_folders is off.
        main: the main agent and everything it opened (root: the workspace); other: sessions other agents
        opened directly (root: tasks_dir). Folders are matched by root, so renaming one in the app keeps it."""
        setting = self.config.get('codex_app_folders')
        if not setting or 'codex' not in self.engines or (not self.config.get('engines') and self.config.get('executor') == 'claude'):
            return None
        names = setting if isinstance(setting, dict) else {}
        return {'main': (names.get('main') or self.text['folder_main'], str(self.root.parent)),
                'other': (names.get('other') or self.text['folder_other'], str(self.root))}

    def sidebar_side(self, job):
        """Which desktop sidebar folder a session belongs in, for both engines: 'main' (AgentFleet) for the main
        agent and every session it opened, 'other' (AgentFleet-Other) for sessions others opened directly."""
        return 'main' if not self.is_worker(self.session_root(job)) or self.session_owner(job) else 'other'

    async def sidebar_project(self, connection, job):
        """Codex app-server project id of the job's sidebar folder, found by root (created if missing), or None.
        Assigning through app-server works while the desktop app is open, unlike editing its state file."""
        folders = self.app_folders()
        if not folders:
            return self.config.get('codex_project_id')
        name, root = folders[self.sidebar_side(job)]
        if root not in self.codex_projects:
            try:
                listed = await connection.request('project/list', {'limit': 200})
                project = next((p for p in listed.get('data') or [] if any(
                    same_path(r.get('path') or '', root) for r in p.get('roots') or [])), None)
                if not project:
                    created = await connection.request('project/create', {
                        'name': name, 'roots': [{'path': root}], 'idempotencyKey': 'agent-fleet-' + root.lower()})
                    project = created.get('project') or created
            except AppServerError:
                return None  # an older Codex without the project API: the state-file filing still applies
            if not (project or {}).get('id'):
                return None
            self.codex_projects[root] = project['id']
        return self.codex_projects[root]

    def folder_threads(self):
        """Codex threads by folder side, forks and their originals included."""
        sides = {'main': [], 'other': []}
        for job in list(self.jobs.values()):
            if job.get('engine') == 'claude':
                continue
            side = self.sidebar_side(job)
            sides[side] += [job[k] for k in ('thread_id', 'forked_from_thread_id') if job.get(k)]
        return sides

    def file_into_app_folders(self, folders, threads):
        """Blocking: file the threads into the app's sidebar folders unless the app is open, since it
        rewrites its state file while running. Returns 'waiting' or 'filed'."""
        codex_home = Path(self.config.get('codex_home') or os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        path = codex_home / STATE_FILE
        if not path.exists() or desktop_app_running():
            return 'waiting'
        state = json.loads(path.read_text(encoding='utf-8'))
        updated, reports, changed = sort_into_folders(
            state, [(name, root, threads[side]) for side, (name, root) in folders.items()],
            int(time.time() * 1000), lambda: str(uuid.uuid4()))
        if changed:
            if desktop_app_running():
                return 'waiting'
            write_state(path, updated, {t: r['project_id'] for r in reports for t in r['assigned']},
                        path.with_name(path.name + '.bak-agent-fleet'),
                        {r['project_id']: r['project_name'] for r in reports})
        return 'filed'

    def idle(self):
        return not any(j['status'] in ('queued', 'running') for j in self.jobs.values())

    # Setup run while the service is busy (typically asked of this computer's own main agent, whose turn keeps
    # it busy) deploys the new code and leaves this marker instead of killing the service mid-task. Once the
    # queue is empty the service relaunches itself on the new code.
    RESTART_MARKER = 'restart-when-idle'

    async def restart_loop(self):
        marker = self.state / self.RESTART_MARKER
        while not self.closing:
            await asyncio.sleep(self.config.get('restart_check_interval', 5))
            if marker.exists() and self.idle() and self.config.get('_config_path'):
                try:
                    marker.unlink()
                except OSError:
                    continue
                self.restart_now()
                return

    def restart_now(self):
        launcher = Path(__file__).with_name('start_service.py')
        detach = ({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS} if os.name == 'nt'
                  else {'start_new_session': True})
        subprocess.Popen([self.config['python_executable'], str(launcher), '--config', self.config['_config_path'],
                          '--after-pid', str(os.getpid())], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, **detach)
        os._exit(0)  # every job is already saved; the launcher starts the new code once this process is gone

    async def folder_loop(self):
        while not self.closing:
            folders = self.app_folders()
            if folders and self.folders_pending:
                try:
                    result = await asyncio.to_thread(self.file_into_app_folders, folders, self.folder_threads())
                    self.folders_status = {'result': result, 'checked_at': now()}
                    self.folders_pending = result == 'waiting'
                except Exception as error:
                    # A file the app changed in an unexpected way: don't retry every few seconds; the next finished task tries again.
                    self.folders_status = {'error': str(error)[-500:], 'checked_at': now()}
                    self.folders_pending = False
            await asyncio.sleep(self.config.get('codex_app_folder_interval', 30))

    def after_job(self, job):
        """When a sub-task finishes, queue a message that wakes the main agent which dispatched it."""
        self.folders_pending = True
        parent = self.jobs.get(job.get('parent_task_id') or '')
        if parent and parent.get('thread_id') and job['status'] in FINISHED and self.config.get('wake_parent', True):
            self.notices.setdefault(self.latest_thread(parent['thread_id']), []).append(job['task_id'])
        for thread in list(self.notices):
            self.flush_notices(thread)

    def flush_notices(self, thread):
        if any(j['status'] in ('queued', 'running') and j.get('thread_id') == thread for j in self.jobs.values()):
            return  # Delivered after the current turn, batched with anything else that finishes meanwhile.
        children = self.notices.pop(thread, [])
        turns = sorted((j for j in self.jobs.values() if j.get('thread_id') == thread), key=lambda j: j['created_at'])
        if not children or not turns:
            return
        streak = 0
        for turn in reversed(turns):
            if not turn.get('auto_wake'):
                break
            streak += 1
        if streak >= self.config.get('max_auto_wakes', 20):
            turns[-1]['latest_message'] = f'Auto wake-up paused after {streak} in a row; send it a message to resume.'
            self.save(turns[-1])
            return
        lines = []
        for child_id in children:
            child = self.jobs[child_id]
            result_file = self.jobs_dir / child_id / 'result.txt'
            text = result_file.read_text(encoding='utf-8')[:1500] if result_file.exists() else ''
            line = self.text['wake_line'].format(name=child.get('name') or self.text['wake_unnamed'], task_id=child_id,
                                                 status=child['status'], result=text or child.get('error') or self.text['wake_none'])
            # A failure can come after real work (e.g. drafts saved, then "model at capacity"); say how far it got.
            if (child['status'] != 'completed' or not text) and child.get('latest_message'):
                line += self.text['wake_progress'].format(progress=child['latest_message'][-800:])
            lines.append(line)
        prompt = self.text['wake_header'] + self.text['wake_intro'] + '\n'.join(lines) + self.text['wake_tail']
        woken = self.submit(prompt, None, self.config.get('wake_timeout_seconds', 1800), None, resume_task_id=turns[-1]['task_id'])
        self.jobs[woken['task_id']].update(auto_wake=True, wakes_for=children)
        self.save(self.jobs[woken['task_id']])

    def submit(self, prompt, workspace, timeout_seconds, idempotency_key, resume_task_id=None, name=None,
               parent_task_id=None, model=None, effort=None, engine=None):
        if self.closing:
            raise ValueError('Service is shutting down')
        if not prompt.strip() or len(prompt) > 100_000:
            raise ValueError('prompt must contain 1 to 100,000 characters')
        if name is not None and not 1 <= len(name.strip()) <= 100:
            raise ValueError('name must contain 1 to 100 characters')
        name = name.strip() if name is not None else None
        if not 10 <= timeout_seconds <= self.config.get('max_timeout_seconds', 7200):
            raise ValueError('timeout_seconds is outside the configured range')
        # parent_task_id is the calling main-agent turn; internal wake-ups and the orchestrator have none.
        if parent_task_id and resume_task_id:
            self.authorize(self.get(resume_task_id), parent_task_id, 'continue')
        elif parent_task_id:
            self.check_dispatch(parent_task_id, name)
        if idempotency_key is not None:
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', idempotency_key):
                raise ValueError('Invalid idempotency_key')
            for job in self.jobs.values():
                if job.get('idempotency_key') == idempotency_key:
                    if (job['prompt'], job['requested_workspace'], job['timeout_seconds'], job.get('resume_task_id'), job.get('requested_engine')) != (prompt, workspace, timeout_seconds, resume_task_id, engine) or (
                            'requested_name' in job and job['requested_name'] != name):
                        raise ValueError('idempotency_key was already used for a different request')
                    return self.public(job)
        if sum(j['status'] in ('queued', 'running') for j in self.jobs.values()) >= 32:
            raise ValueError('Task queue is full; retry after a task finishes')
        parent = self.get(resume_task_id) if resume_task_id else None
        explicit_engine = engine
        if engine is not None and engine not in self.engines:
            raise ValueError('engine must be one of the configured engines')
        main = self.main_for(workspace, name)
        registered = self.profile['agents'].get(name or '', {})
        registered_engine = registered.get('engine', self.engines[0]) if registered.get('type') == 'main' else None
        engine = (parent or {}).get('engine') or (main or {}).get('engine') or registered_engine or (self.get(parent_task_id).get('engine') if parent_task_id else None) or engine or self.engines[0]
        if engine not in self.engines:
            raise ValueError('engine must be one of the configured engines')
        if explicit_engine and explicit_engine != engine and (parent or main or registered_engine or parent_task_id):
            raise ValueError('Cannot override the engine of a session, main agent or parent task')
        if parent:
            if parent['engine'] not in self.engines:
                raise ValueError('Cannot resume a session with a different agent engine; create a new session')
            if parent['status'] in ('queued', 'running') or not parent.get('thread_id'):
                raise ValueError('Wait for the task to finish and receive a thread_id before continuing')
            if any(j['status'] in ('queued', 'running') and j.get('thread_id') == parent['thread_id'] for j in self.jobs.values()):
                raise ValueError('This session already has pending work; wait for it to finish')
        task_id = str(uuid.uuid4())
        target = Path(parent['workspace']).resolve() if parent else (
            Path(main['workspace']).resolve() if main and main.get('workspace') else
            Path(workspace).resolve() if workspace else self.root / task_folder_name(task_id, name))
        if not any(target.is_relative_to(root) for root in self.roots):
            raise ValueError('workspace is outside allowed_roots in the server configuration')
        # Renaming the configured main agent changes its title while preserving
        # the existing engine session and workspace on continuation.
        main = self.main_for(str(target))
        final_name = main['name'] if main and main.get('name') else (
            parent.get('name') if parent else name and name.strip())
        if parent_task_id and not parent:
            self.check_dispatch(parent_task_id, final_name)
        # A registered agent is one session: a second one with the same name would split its history.
        if not parent and (final_name in self.profile['agents'] or main):
            existing = self.named_session(final_name)
            if existing:
                raise ValueError(f'「{final_name}」 already has a session (task_id={existing["task_id"]}); continue it with '
                                 'continue_task instead of opening another session with the same name')
        target.mkdir(parents=True, exist_ok=True)
        if not target.is_dir():
            raise ValueError('workspace must be a directory')
        job = dict(task_id=task_id, prompt=prompt, requested_workspace=workspace,
                   engine=engine, requested_engine=explicit_engine, requested_name=name,
                   workspace=str(target), timeout_seconds=timeout_seconds,
                   idempotency_key=idempotency_key, status='queued', created_at=now())
        if parent:
            job.update(resume_task_id=resume_task_id, thread_id=self.latest_thread(parent['thread_id']))
        if final_name:
            job['name'] = final_name
        if parent_task_id:
            job['parent_task_id'] = parent_task_id
        # Normally unset: Codex's own default (the latest model at its configured effort), like a new window.
        # A continuation keeps the session's earlier choice unless told otherwise.
        for key, value in (('model', model), ('effort', effort)):
            chosen = value or (parent or {}).get(key)
            if chosen:
                job[key] = chosen
        self.jobs[task_id] = job
        self.save(job)
        self.queues[engine].put_nowait(task_id)
        return self.public(job)

    async def kill(self, process):
        if process.returncode is not None:
            return
        if os.name == 'nt':
            killer = await asyncio.create_subprocess_exec('taskkill.exe', '/PID', str(process.pid), '/T', '/F', stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), 10)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def cancel(self, task_id, caller=None):
        job = self.get(task_id)
        self.authorize(job, caller, 'cancel')
        if job['status'] in ('queued', 'running'):
            job.update(status='cancelled', finished_at=now())
            self.save(job)
            self.quota_wakes[job['engine']].set()
            if task_id in self.processes:
                await self.kill(self.processes[task_id])
        return self.public(job)

    def mark_running(self, job):
        """The turn is really about to start. Until then the task shows as queued, so a quota check that
        sends it back in line doesn't make the dashboard or the busy list flicker."""
        if job['status'] != 'queued':
            raise RuntimeError('The task was cancelled before it started')
        job.update(status='running', started_at=now())
        self.save(job)

    async def run_job(self, job):
        if job['status'] != 'queued':
            return
        if job.get('engine') == 'claude':
            await claude_executor.run(self, job)
            return
        folder = self.jobs_dir / job['task_id']
        executable = self.config.get('codex_executable') or shutil.which('codex')
        if not executable:
            raise RuntimeError('Codex CLI executable not found')
        launcher = self.config.get('codex_launcher_executable', executable)
        launcher_args = self.config.get('codex_launcher_args', [])
        # One app-server per task; exiting releases the session's writer lock.
        command = [launcher, *launcher_args, 'app-server']
        env = os.environ.copy()
        # This bridge uses Codex's existing ChatGPT login, not an inherited API key.
        env.pop('OPENAI_API_KEY', None)
        env.pop('CODEX_API_KEY', None)
        options = {'creationflags': 0x08000000} if os.name == 'nt' else {'start_new_session': True}
        with (folder / 'stderr.log').open('wb') as error_log, (folder / 'events.jsonl').open('wb') as event_log:
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=error_log, env=env, limit=33_554_432, **options))
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                # If shutdown races process creation, retrieve and stop the child.
                process = await spawn
                await self.kill(process)
                raise
            self.processes[job['task_id']] = process
            try:
                if job['status'] not in ('queued', 'running') or self.closing:
                    return

                runner = self.drive_app_server(job, process, event_log, folder)

                try:
                    job['exit_code'] = await asyncio.wait_for(runner, job['timeout_seconds'])
                except asyncio.TimeoutError:
                    await self.kill(process)
                    if job['status'] in ('queued', 'running'):
                        job.update(status='timed_out', error='Task exceeded timeout_seconds')
                except QuotaExhausted as paused:
                    if job['status'] == 'queued':
                        job['latest_message'] = str(paused)  # Still in line untouched: no session was started.
                    raise
                if job['status'] == 'running':
                    success = job.get('exit_code') == 0 and not job.get('error') and (folder / 'result.txt').exists()
                    job['status'] = 'completed' if success else 'failed'
                    if not success and not job.get('error'):
                        error_log.flush()
                        job['error'] = (folder / 'stderr.log').read_text(encoding='utf-8', errors='replace')[-2000:] or 'Codex did not produce a final result'
            finally:
                await self.kill(process)
                self.processes.pop(job['task_id'], None)
                if job['status'] != 'queued':
                    job.setdefault('finished_at', now())
                self.save(job)

    async def drive_app_server(self, job, process, event_log, folder):
        """Run one turn through `codex app-server`; return 0 when the turn completed, else 1."""
        def log_line(line):
            event_log.write(line)
            event_log.flush()

        auto_approve = bool(self.config.get('auto_approve', False))
        sandbox = self.config.get('sandbox', 'workspace-write')
        # "never" makes Codex refuse anything needing approval; "on-request" lets it ask, and we say yes.
        policy = {'approvalPolicy': 'on-request' if auto_approve else 'never', 'sandbox': sandbox}
        if self.is_worker(job):
            # Workers have full access and could find the token on disk; tell them plainly not to use it.
            policy['developerInstructions'] = self.worker_rules()
        else:
            policy['developerInstructions'] = ('This turn runs Codex with sandbox=' + sandbox +
                '. Current main agent name: ' + json.dumps(job.get('name'), ensure_ascii=False) +
                '. These runtime settings override older name/engine/permission descriptions in preserved workspace instructions.')
        connection = AppServerConnection(process, log_line, auto_approve)
        turn_done = asyncio.get_running_loop().create_future()

        def on_notification(message):
            params = message.get('params') or {}
            if params.get('threadId') not in (None, job.get('thread_id')):
                return
            item = params.get('item') or {}
            if message['method'] == 'item/completed' and item.get('type') == 'agentMessage':
                job['latest_message'] = (item.get('text') or '')[-4000:]
                self.save(job)
            elif message['method'] == 'turn/completed' and not turn_done.done():
                turn_done.set_result(params.get('turn') or {})

        connection.notification_handlers.append(on_notification)
        turn, quiet = None, False
        try:
            # File sessions under the sidebar folders (or one configured project): app-server's experimental API.
            experimental = bool(self.app_folders() or self.config.get('codex_project_id'))
            await connection.request('initialize', {'clientInfo': {
                'name': 'codex_task_dispatcher', 'title': 'Codex Task Dispatcher', 'version': '2.0'},
                **({'capabilities': {'experimentalApi': True}} if experimental else {})})
            await connection.notify('initialized')
            try:
                await self.check_quota(connection, job, log_line)
            except QuotaExhausted:
                quiet = True  # Nothing was started, so let the app-server exit normally.
                raise
            self.mark_running(job)
            suffix = self.title_suffix(job)
            project = await self.sidebar_project(connection, job) if experimental else None
            if job.get('resume_task_id'):
                forked = await self.resume_or_fork(connection, job, policy)
                name = None if forked else self.changed_title(job)
                if project and self.app_folders():
                    try:  # also files sessions started before this, and forks
                        await connection.request('thread/metadata/update', {'threadId': job['thread_id'], 'projectId': project})
                    except AppServerError:
                        pass
            else:
                started = await connection.request('thread/start', {
                    'cwd': job['workspace'], **policy, **({'model': job['model']} if job.get('model') else {}),
                    **({'projectId': project} if project else {})})
                job['thread_id'] = started['thread']['id']
                self.save(job)
                name = (job.get('name') or ' '.join(job['prompt'].split())[:40]) + suffix
            if name:
                try:
                    await connection.request('thread/name/set', {'threadId': job['thread_id'], 'name': name})
                    root = self.session_root(job)
                    root['title_set'] = name
                    self.save(root)
                except AppServerError:
                    pass  # The name is cosmetic; the task can still run.
            turn_input = job['prompt']
            if not self.is_worker(job) and (self.config.get('main_agent_name') or self.profile['computer'].get('manager_name')):
                turn_input = self.main_agent_note(job) + '\n\n' + turn_input
            turn_params = {'threadId': job['thread_id'], 'input': [{'type': 'text', 'text': turn_input}],
                           'approvalPolicy': policy['approvalPolicy']}  # turn/start takes no developerInstructions
            turn_params.update({k: job[k] for k in ('model', 'effort') if job.get(k)})
            if sandbox == 'danger-full-access':
                turn_params['sandboxPolicy'] = {'type': 'dangerFullAccess'}
            elif sandbox == 'workspace-write' and self.config.get('shared_dir'):
                # The shared folder is where tasks hand files to each other, so it must be writable too.
                turn_params['sandboxPolicy'] = {'type': 'workspaceWrite', 'writableRoots': [self.config['shared_dir']]}
            await connection.request('turn/start', turn_params)
            done, _ = await asyncio.wait({turn_done, connection.reader}, return_when=asyncio.FIRST_COMPLETED)
            if turn_done not in done:
                raise RuntimeError('Codex app-server exited before the turn finished')
            turn = turn_done.result()
        finally:
            connection.reader.cancel()
            # Let the app-server exit before the task is reported finished, so the session's writer lock
            # is already released when the caller continues it or the user opens it in the desktop app.
            # On timeout, cancellation or errors, stop it at once instead.
            try:
                if turn is None and not quiet:
                    raise OSError
                process.stdin.close()
                await asyncio.wait_for(process.wait(), 10)
            except (asyncio.TimeoutError, OSError):
                await self.kill(process)
        messages = [i for i in turn.get('items') or [] if i.get('type') == 'agentMessage']
        final = [i for i in messages if i.get('phase') == 'final_answer'] or messages
        (folder / 'result.txt').write_text((final[-1].get('text') or '') if final else '', encoding='utf-8')
        if turn.get('status') != 'completed':
            error = turn.get('error') or {}
            job['error'] = str(error.get('message') or error or f"Turn ended with status {turn.get('status')}")[-2000:]
            return 1
        return 0

    async def check_quota(self, connection, job, log_line):
        """The owner's rule: don't start a task while the Codex 5-hour (or weekly) quota is used up. Raises
        QuotaExhausted before any session is touched. A failed call or an unknown answer never blocks."""
        if not self.config.get('quota_guard', True):
            return
        try:
            result = await asyncio.wait_for(connection.request('account/rateLimits/read'), QUOTA_CHECK_TIMEOUT)
            if not isinstance(result, dict) or not isinstance(result.get('rateLimits'), dict):
                raise ValueError('unexpected answer: ' + json.dumps(result, ensure_ascii=False)[:500])
        except (AppServerError, RuntimeError, ValueError, asyncio.TimeoutError) as error:
            log_line((json.dumps({'dispatcher': 'quota check skipped', 'error': str(error) or type(error).__name__},
                                 ensure_ascii=False) + '\n').encode('utf-8'))
            result = None
        blocked = quota_exhausted(result, self.config.get('quota_guard_threshold', 100), self.language)
        if blocked:
            raise QuotaExhausted(*blocked, self.language)
        self.quota_recovered(job, 'Codex')

    def quota_recovered(self, job, engine):
        """The job is about to start with quota left: clear its waiting note and, after a pause, say it's back."""
        if (job.get('latest_message') or '').startswith(QUOTA_NOTES):
            job.pop('latest_message')
            self.save(job)
        key = job['engine']
        self.quota_waits.pop(key, None)
        self.quota_wait = {**next(iter(self.quota_waits.values())), 'engines': dict(self.quota_waits)} if self.quota_waits else None
        if key in self.quota_pauses:
            self.quota_pauses.discard(key)
            self.quota_paused = bool(self.quota_pauses)
            self.notify(self.text['quota_back_title'].format(engine=engine),
                        self.text['quota_back'].format(name=job.get('name') or job['task_id'][:8]),
                        task_ids=[job['task_id']], sender='dispatcher')

    def check_claude_quota(self, job):
        """Claude's counterpart of check_quota. Claude Code reports its limits only while running, so the last
        rejection it reported holds the queue until its reset time; with no reset time known, the next try after
        the poll interval finds out again."""
        limit = self.claude_limit
        if limit and self.config.get('quota_guard', True):
            resets = limit.get('resetsAt') if isinstance(limit.get('resetsAt'), (int, float)) else None
            if resets is None or resets > time.time():
                if resets is None:
                    self.claude_limit = None
                kind = str(limit.get('rateLimitType') or '')
                windows = self.text['windows']
                label = windows['secondary'] if ('day' in kind or 'week' in kind) else windows['primary']
                paused = QuotaExhausted([label], resets, self.language, 'Claude')
                if job['status'] == 'queued':
                    job['latest_message'] = str(paused)
                    self.save(job)
                raise paused
            self.claude_limit = None
        self.quota_recovered(job, 'Claude')

    async def wait_for_quota(self, job, paused):
        """Hold this engine's queue behind this job until the quota should be back. The orchestrator hears about
        the pause once, not on every re-check. Cancelling the job or stopping the service ends the wait at once."""
        if job['status'] != 'queued':
            return
        until = datetime.fromtimestamp(paused.resets_at, timezone.utc).isoformat() if paused.resets_at else None
        engine = job['engine']
        self.quota_waits[engine] = {'engine': engine, 'until': until, 'message': str(paused), 'task_id': job['task_id']}
        self.quota_wait = {**next(iter(self.quota_waits.values())), 'engines': dict(self.quota_waits)}
        if engine not in self.quota_pauses:
            self.quota_pauses.add(engine)
            self.quota_paused = True
            queued = sum(j['status'] == 'queued' and j['engine'] == engine for j in self.jobs.values())
            self.notify(self.text['quota_paused_title'].format(engine=paused.engine) + (self.text['quota_paused_until'].format(time=local_clock(paused.resets_at))
                                                           if paused.resets_at else ''),
                        self.text['quota_paused'].format(reason=paused, queued=queued),
                        task_ids=[job['task_id']], sender='dispatcher')
        delay = QUOTA_RECHECK_SECONDS if paused.resets_at is None else max(paused.resets_at - time.time(), 0) + QUOTA_SLACK_SECONDS
        deadline = time.monotonic() + min(delay, QUOTA_MAX_WAIT_SECONDS)
        while job['status'] == 'queued' and not self.closing and deadline > time.monotonic():
            self.quota_wakes[engine].clear()
            try:
                await asyncio.wait_for(self.quota_wakes[engine].wait(), deadline - time.monotonic())
                break
            except asyncio.TimeoutError:
                break

    async def resume_or_fork(self, connection, job, policy):
        """Load an existing session. The desktop app keeps every chat it has opened locked until it fully
        exits; if the session stays held, fork it (as Claude workers do), or fail when fork_when_busy is off. Returns True if forked."""
        deadline = asyncio.get_running_loop().time() + self.config.get('writer_wait_seconds', 10)
        while True:
            try:
                # Re-apply the current permissions and folder, so older sessions pick up config changes
                # and sessions whose folder was moved continue in the new place.
                await connection.request('thread/resume', {'threadId': job['thread_id'], 'cwd': job['workspace'], **policy})
                return False
            except AppServerError as error:
                if 'active writer' not in str(error):
                    raise
                if asyncio.get_running_loop().time() < deadline:
                    job['latest_message'] = 'Waiting: this session is open in another Codex window.'
                    self.save(job)
                    await asyncio.sleep(5)
                    continue
                if not self.config.get('fork_when_busy', True):
                    raise RuntimeError('This session is held by another Codex window. The desktop app keeps chats it '
                                       'has opened locked until it fully exits. / 这个会话在桌面 App 里打开过，App 会一直占用它，'
                                       '直到 Codex 完全退出；请开一个新任务，或退出 Codex 后再继续。') from error
                break
        original = job['thread_id']
        forked = await connection.request('thread/fork', {'threadId': original, 'cwd': job['workspace'], **policy})
        job.update(forked_from_thread_id=original, thread_id=forked['thread']['id'])
        self.save(job)
        base = job.get('name') or 'Codex task'
        try:
            await connection.request('thread/name/set', {
                'threadId': job['thread_id'], 'name': self.text['fork_title'].format(
                    base=base, time=datetime.now().strftime('%m-%d %H:%M'), suffix=self.title_suffix(job))})
        except AppServerError:
            pass
        return True

    async def work(self, engine=None):
        engine = engine or self.engines[0]
        queue = self.queues[engine]
        while True:
            task_id = await queue.get()
            job = self.get(task_id)
            try:
                while not self.closing:
                    try:
                        await self.run_job(job)
                        break
                    except QuotaExhausted as paused:
                        # Retry this same job before taking the next, so the order holds and nothing else burns quota.
                        await self.wait_for_quota(job, paused)
            except asyncio.CancelledError:
                if job['status'] in ('running', 'queued'):
                    job.update(status='interrupted', finished_at=now(), error='Service shut down')
                    self.save(job)
                raise
            except Exception as error:
                if job['status'] in ('queued', 'running'):  # app-server tasks are still queued before their turn
                    job.update(status='failed', finished_at=now(), error=str(error)[-2000:])
                    self.save(job)
            finally:
                self.quota_waits.pop(engine, None)
                self.quota_wait = {**next(iter(self.quota_waits.values())), 'engines': dict(self.quota_waits)} if self.quota_waits else None
                queue.task_done()
            try:
                self.after_job(job)
            except Exception:
                pass  # A failed wake-up must never stop the queue.

    async def shutdown(self):
        self.closing = True
        for task in set([self.worker, self.folder_task, *self.workers.values()]):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        for job in self.jobs.values():
            if job['status'] == 'queued':
                job.update(status='interrupted', finished_at=now(), error='Service shut down before task started')
                self.save(job)


FILE_KINDS = {'image': {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg'},
              'video': {'.mp4', '.webm', '.mov', '.m4v', '.mkv'},
              'audio': {'.mp3', '.wav', '.m4a', '.ogg', '.flac', '.aac'},
              'text': {'.txt', '.md', '.json', '.log', '.csv', '.html', '.srt', '.vtt'}}
CONTENT_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp',
                 '.bmp': 'image/bmp', '.svg': 'image/svg+xml', '.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime',
                 '.m4v': 'video/mp4', '.mkv': 'video/x-matroska', '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.m4a': 'audio/mp4',
                 '.ogg': 'audio/ogg', '.flac': 'audio/flac', '.aac': 'audio/aac', '.txt': 'text/plain; charset=utf-8',
                 '.md': 'text/plain; charset=utf-8', '.json': 'application/json', '.log': 'text/plain; charset=utf-8',
                 '.csv': 'text/plain; charset=utf-8', '.html': 'text/plain; charset=utf-8', '.srt': 'text/plain; charset=utf-8',
                 '.vtt': 'text/vtt', '.pdf': 'application/pdf'}


def output_roots(config):
    """Folders whose files the orchestrator may read: what agents produce (shared) and their task folders."""
    roots = []
    for key in ('shared_dir', 'tasks_dir'):
        if config.get(key):
            root = Path(config[key]).resolve()
            if root not in roots:
                roots.append(root)
    return roots


def resolve_output_path(config, raw):
    """An absolute path, or one relative to the shared folder, that must stay inside an output root."""
    roots = output_roots(config)
    if not roots:
        raise ValueError('this worker has no shared or tasks folder')
    path = Path(raw) if raw and Path(raw).is_absolute() else roots[0] / (raw or '')
    path = path.resolve()
    if not any(path == root or path.is_relative_to(root) for root in roots):
        raise ValueError('path is outside the shared and tasks folders')
    return path


def list_output_dir(config, raw):
    path = resolve_output_path(config, raw)
    if not path.is_dir():
        raise ValueError('not a folder')
    entries = []
    for child in path.iterdir():
        try:
            stat = child.stat()
            if child.is_dir():
                entries.append({'name': child.name, 'path': str(child), 'type': 'dir', 'mtime': stat.st_mtime})
            elif child.is_file():
                kind = next((k for k, exts in FILE_KINDS.items() if child.suffix.lower() in exts), 'other')
                entries.append({'name': child.name, 'path': str(child), 'type': 'file', 'kind': kind,
                                'size': stat.st_size, 'mtime': stat.st_mtime})
        except OSError:
            continue  # vanished or unreadable
    entries.sort(key=lambda e: (e['type'] != 'dir', -e['mtime']))
    parent = path.parent if any(path.parent == r or path.parent.is_relative_to(r) for r in output_roots(config)) and path not in output_roots(config) else None
    return {'path': str(path), 'parent': str(parent) if parent else None, 'roots': [str(r) for r in output_roots(config)],
            'entries': entries[:1000], 'truncated': len(entries) > 1000}


def parse_range(header, size):
    """(start, end) for a single 'bytes=a-b' range, None for no/unsupported range, ValueError if unsatisfiable."""
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', (header or '').strip())
    if not match or match.group(1) == match.group(2) == '':
        return None
    if match.group(1) == '':
        start, end = max(size - int(match.group(2)), 0), size - 1
    else:
        start, end = int(match.group(1)), min(int(match.group(2)) if match.group(2) else size - 1, size - 1)
    if start >= size or start > end:
        raise ValueError('range not satisfiable')
    return start, end


def file_response(path, range_header=None, download=False):
    """Stream a file with Range support, so videos can be scrubbed in the browser."""
    size = path.stat().st_size
    headers = {'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
               'Content-Disposition': ("attachment" if download else "inline") + "; filename*=UTF-8''" + quote(path.name)}
    try:
        span = parse_range(range_header, size)
    except ValueError:
        return Response(status_code=416, headers={**headers, 'Content-Range': f'bytes */{size}'})
    start, end = span or (0, size - 1)

    def chunks():
        with path.open('rb') as handle:
            handle.seek(start)
            left = end - start + 1
            while left > 0:
                block = handle.read(min(1 << 20, left))
                if not block:
                    break
                left -= len(block)
                yield block
    headers['Content-Length'] = str(max(end - start + 1, 0))
    if span:
        headers['Content-Range'] = f'bytes {start}-{end}/{size}'
    return StreamingResponse(chunks() if size else iter(()), status_code=206 if span else 200,
                             media_type=CONTENT_TYPES.get(path.suffix.lower(), 'application/octet-stream'), headers=headers)


class BearerMiddleware:
    # The dashboard page itself holds no data; it asks for the token and sends it on every API call.
    # Pairing answers only while a window opened with the token is running, and hands out the token only
    # for its one-time code.
    PUBLIC = {('GET', '/dashboard'), ('GET', '/pair/info'), ('POST', '/pair/claim')}

    def __init__(self, app, token):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http' and (scope.get('method'), scope.get('path')) not in self.PUBLIC:
            headers = dict(scope['headers'])
            supplied = headers.get(b'authorization', b'').decode('latin1')
            if not hmac.compare_digest(supplied.encode('utf-8'), ('Bearer ' + self.token).encode('utf-8')):
                response = JSONResponse({'error': 'Bearer token required'}, status_code=401,
                    headers={'WWW-Authenticate': 'Bearer', 'Cache-Control': 'no-store'})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(config):
    token = Path(config['token_file']).read_text(encoding='utf-8').strip()
    if len(token) < 32:
        raise ValueError('A strong authentication token is required')
    manager = TaskManager(config)

    busy_note = ('continuing such a session automatically forks a copy with the full history (thread_id changes, '
                 'forked_from_thread_id is set, the copy is named "…（续 time）" or "… (cont. time)"), and later continue_task calls follow the newest copy.'
                 if config.get('fork_when_busy', True) else
                 'continuing such a session fails with an explanation; tell the human, or start a new task instead.')
    mcp = FastMCP('Codex Task Dispatcher',
        instructions='Submit tasks to the configured Codex or Claude Code engines on the server computer. Main-agent names select their engine; children inherit their parent engine and resumed sessions keep their original engine. Direct submissions may specify an enabled engine. submit_task creates a new session; continue_task(task_id, prompt) reuses an existing dispatched session with its conversation history and workspace. Wait for prior work in the session to finish before continuing. Paths refer to the server computer. Use idempotency_key when retrying submission. Poll get_task every 5–15 seconds. Completed means a final result was produced; inspect failed/timed_out/interrupted errors. Tasks run one at a time per engine; different engines may run concurrently. Always include the returned desktop_link as a clickable Markdown link when reporting a task to the human, so they can open it without contacting another agent. desktop_url opens the local Codex app on the computer where clicked. If the human wants the chat shown on the server desktop, call open_task after the task finishes; it opens that chat directly without restarting the app. The user does not need to ask this dispatcher implementation chat to open it. Sessions appear in the Codex desktop app sidebar under their name. The desktop app keeps chats it has opened locked until it fully exits; ' + busy_note + ' A read-only live view of all sessions, which never locks them, is at /dashboard on this server. get_profile describes this computer (hardware, role, software, accounts) and its long-lived agents; keep it current with update_computer_profile and register_agent. A main agent can continue, cancel or open only the sessions it opened; the orchestrator controls every session and is the only one that sets an agent\'s can_dispatch / locked with register_agent.',
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=config['allowed_hosts'], allowed_origins=config.get('allowed_origins', [])))

    @mcp.tool()
    async def submit_task(prompt: str, workspace: str | None = None,
                          timeout_seconds: int = 1800, idempotency_key: str | None = None,
                          name: str | None = None, model: str | None = None, effort: str | None = None, engine: str | None = None,
                          ctx: Context = None) -> dict:
        """Dispatch work to an enabled engine; return task_id immediately. Normally omit engine: a main agent selects its own engine, children inherit their parent, and other tasks use the default. An explicit engine is for direct tasks only. Omit workspace for a new isolated folder. An explicit workspace must be an absolute path on the server within allowed_roots. Use a stable unique idempotency_key to avoid duplicate tasks after network retries. name is the session title shown in the Codex desktop app (defaults to the start of the prompt). When a main agent dispatches, it is woken automatically with the result once the task finishes. model / effort: normally OMIT both, so the task uses Codex's default (the latest model at high effort, same as opening a new window); set them only under specific conditions the human asked for."""
        if workspace is not None and not Path(workspace).is_absolute():
            raise ValueError('workspace must be an absolute path on the server')
        return manager.submit(prompt, workspace, timeout_seconds, idempotency_key, name=name,
                              parent_task_id=manager.caller(ctx), model=model, effort=effort, engine=engine)

    @mcp.tool()
    async def continue_task(task_id: str, prompt: str, timeout_seconds: int = 1800,
                            idempotency_key: str | None = None, model: str | None = None, effort: str | None = None,
                            ctx: Context = None) -> dict:
        """Continue the Codex session of a finished dispatched task, preserving its conversation history and workspace. Return a new task_id for this turn. You can reference any finished task in that session; continuation uses its latest saved history. A session the human has opened in the desktop app stays locked until Codex fully exits (see the server instructions for what happens then). Wait until the session has no pending task. Only sessions created by this service can be continued; a main agent can continue only sessions it opened, and never a locked agent's. model / effort: normally OMIT both; set them only when the session keeps failing with "Selected model is at capacity" or the human asks. The override applies to this turn, and later continuations of the session inherit it."""
        return manager.submit(prompt, None, timeout_seconds, idempotency_key, resume_task_id=task_id,
                              parent_task_id=manager.caller(ctx), model=model, effort=effort)

    @mcp.tool()
    async def get_task(task_id: str, include_result: bool = True) -> dict:
        """Read task status, latest message, and the final result when finished. Poll every 5–15 seconds. Results persist across service restarts."""
        return manager.public(manager.get(task_id), include_result)

    @mcp.tool()
    async def list_tasks(limit: int = 20) -> dict:
        """List recent tasks, newest first, without their full results."""
        if not 1 <= limit <= 100:
            raise ValueError('limit must be between 1 and 100')
        jobs = sorted(manager.jobs.values(), key=lambda j: j['created_at'], reverse=True)
        return {'tasks': [manager.public(j) for j in jobs[:limit]]}

    @mcp.tool()
    async def cancel_task(task_id: str, ctx: Context = None) -> dict:
        """Cancel queued or running work and stop its local process tree. Changes already made by a task are preserved. A main agent can cancel only sessions it opened."""
        return await manager.cancel(task_id, manager.caller(ctx))

    @mcp.tool()
    async def open_task(task_id: str, ctx: Context = None) -> dict:
        """Open a finished task's conversation directly in the Codex desktop app on the SERVER COMPUTER, through the official chat deep link. No app restart is needed. Only this dispatcher's sessions can be opened. Wait for all pending work in the session to finish. Returns requested, not a guarantee that the screen rendered. To let the human open it on the same computer themselves, provide desktop_link from get_task instead."""
        return manager.open_desktop(task_id, manager.caller(ctx))

    @mcp.tool()
    async def get_profile(refresh: bool = False) -> dict:
        """This computer's profile: detected hardware (CPU, RAM, GPU/VRAM, disks), network (egress IP, Tailscale), the hand-written role, notes, key software and account login status, the registered long-lived agents with their roles, tags and latest status, and what is running now. Hardware is detected once and saved (see machine.checked_at); pass refresh=true only when it may have changed, e.g. after a hardware or network change."""
        return await manager.full_profile(refresh)

    @mcp.tool()
    async def update_computer_profile(role: str | None = None, notes: str | None = None,
                                      software: list[str] | None = None, accounts: list[dict] | None = None,
                                      manager_name: str | None = None, ctx: Context = None) -> dict:
        """Update the hand-written part of this computer's profile. Only the fields you pass are replaced. role: what this computer is for (e.g. "视频剪辑机，有 GPU"). software: key installed apps with versions if known. accounts: [{"site": "bilibili", "account": "name", "status": "logged_in|logged_out|needs_verification", "expires": "...", "checked_at": "ISO time"}]. Never include passwords, tokens or cookies. manager_name: orchestrator only; the name the orchestrator and the human use for this computer's main agent when managing the fleet (e.g. "经理cris"), separate from the session title shown in the app; "" clears it."""
        return manager.update_computer(manager.caller(ctx), role=role, notes=notes, software=software, accounts=accounts,
                                       manager_name=manager_name)

    @mcp.tool()
    async def register_agent(name: str, role: str, tags: list[str] | None = None, agent_type: str = 'resident',
                             can_dispatch: bool | None = None, locked: bool | None = None, ctx: Context = None) -> dict:
        """Add or update a long-lived agent in this computer's profile. name must match the session name used with submit_task. role: what it is responsible for. tags: capabilities such as gpu, 网页, app, 剪映, bilibili. agent_type: main (this computer's 主agent export) or resident. Do not register one-off tasks. can_dispatch / locked: orchestrator only; omit them to keep the current values. can_dispatch=false stops that agent from opening new sessions; locked=true lets only the orchestrator continue, cancel or open its sessions."""
        return manager.register_agent(name, role, tags, agent_type, can_dispatch, locked, manager.caller(ctx))

    @mcp.tool()
    async def notify_orchestrator(title: str, summary: str, needs_human: bool = False,
                                  task_ids: list[str] | None = None, ctx: Context = None) -> dict:
        """Tell the orchestrator (and through it the human) that a goal is finished, has failed for good, or needs a person (login, verification, a decision). title: one short line. summary: the final report, i.e. what was done, results and where they are. needs_human: true if someone must act. Do not use it for intermediate progress."""
        parent = manager.jobs.get(manager.caller(ctx) or '')
        return manager.notify(title, summary, needs_human, task_ids, sender=(parent or {}).get('name'))

    @mcp.tool()
    async def remove_agent(name: str, ctx: Context = None) -> dict:
        """Remove an agent from this computer's profile (its sessions and history are kept)."""
        return manager.remove_agent(name, manager.caller(ctx))

    @mcp.custom_route('/health', methods=['GET'])
    async def health(request):
        return JSONResponse({'service': 'codex-task-mcp', 'status': 'ok', 'pid': os.getpid(), 'version': manager.deployed_version(),
                             'quota_wait_until': (manager.quota_wait or {}).get('until')})

    @mcp.custom_route('/pair/start', methods=['POST'])
    async def pair_start(request):
        return JSONResponse(manager.start_pairing(), headers={'Cache-Control': 'no-store'})

    @mcp.custom_route('/pair/info', methods=['GET'])
    async def pair_info(request):
        if not manager.pairing_open():
            return JSONResponse({'pairing': False}, status_code=404)
        return JSONResponse(manager.pairing_info(), headers={'Cache-Control': 'no-store'})

    @mcp.custom_route('/pair/claim', methods=['POST'])
    async def pair_claim(request):
        if not manager.pairing_open():
            return JSONResponse({'error': 'No pairing window is open; start one on the worker'}, status_code=404)
        try:
            code = (await request.json()).get('code')
        except (ValueError, AttributeError):
            code = None
        info = manager.claim_pairing(code)
        if info is None:
            return JSONResponse({'error': 'Wrong pairing code', 'window_open': manager.pairing_open()}, status_code=403)
        return JSONResponse(info, headers={'Cache-Control': 'no-store'})

    # Read-only dashboard. Viewing never loads a session into Codex, so it takes no writer lock.
    @mcp.custom_route('/dashboard', methods=['GET'])
    async def dashboard(request):
        page = Path(__file__).with_name('dashboard.html').read_text(encoding='utf-8')
        return HTMLResponse(page, headers={'Cache-Control': 'no-store'})

    @mcp.custom_route('/api/tasks', methods=['GET'])
    async def api_tasks(request):
        jobs = sorted(manager.jobs.values(), key=lambda j: j['created_at'], reverse=True)[:300]
        # The board groups sessions into the same two folders as the desktop sidebars (sidebar_side).
        return JSONResponse({'computer': config.get('computer_name') or os.environ.get('COMPUTERNAME') or platform.node(),
                             'folders': {'main': manager.root.parent.name, 'other': manager.root.name},
                             'tasks': [dict(manager.public(j, include_result=True), folder=manager.sidebar_side(j)) for j in jobs]},
                            headers={'Cache-Control': 'no-store'})

    @mcp.custom_route('/api/profile', methods=['GET'])
    async def api_profile(request):
        profile = await manager.full_profile(request.query_params.get('refresh') == '1')
        return JSONResponse(profile, headers={'Cache-Control': 'no-store'})

    @mcp.custom_route('/api/notifications', methods=['GET'])
    async def api_notifications(request):
        try:
            after = int(request.query_params.get('after', '0'))
        except ValueError:
            after = 0
        items = [n for n in manager.notifications if n['id'] > after][-100:]
        return JSONResponse({'latest_id': manager.notifications[-1]['id'] if manager.notifications else 0,
                             'notifications': items}, headers={'Cache-Control': 'no-store'})

    @mcp.custom_route('/api/transcript', methods=['GET'])
    async def api_transcript(request):
        try:
            entries = manager.transcript(request.query_params.get('thread_id', ''))
        except ValueError as error:
            return JSONResponse({'error': str(error)}, status_code=404)
        return JSONResponse({'entries': entries}, headers={'Cache-Control': 'no-store'})

    # Read-only access to what agents produce, so the orchestrator can preview or fetch it. Only the
    # shared and tasks folders are visible; anything resolving outside them (.., links) is refused.
    @mcp.custom_route('/api/files', methods=['GET'])
    async def api_files(request):
        try:
            return JSONResponse(list_output_dir(config, request.query_params.get('path', '')), headers={'Cache-Control': 'no-store'})
        except (ValueError, OSError) as error:
            return JSONResponse({'error': str(error)}, status_code=404)

    @mcp.custom_route('/api/file', methods=['GET'])
    async def api_file(request):
        try:
            path = resolve_output_path(config, request.query_params.get('path', ''))
            if not path.is_file():
                raise ValueError('not a file')
        except (ValueError, OSError) as error:
            return JSONResponse({'error': str(error)}, status_code=404)
        return file_response(path, request.headers.get('range'), request.query_params.get('download') == '1')

    app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        # Job workers live for the service lifetime, not one MCP request/session.
        async with mcp.session_manager.run():
            manager.workers = {engine: asyncio.create_task(manager.work(engine)) for engine in manager.engines}
            manager.worker = manager.workers[manager.engines[0]]
            manager.folder_task = asyncio.create_task(manager.folder_loop())
            manager.restart_task = asyncio.create_task(manager.restart_loop())
            try:
                yield
            finally:
                await manager.shutdown()

    app.router.lifespan_context = lifespan
    return BearerMiddleware(app, token), manager


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    default_config = os.environ.get('AGENT_FLEET_CONFIG') or str(default_data_dir() / 'config.json')
    parser.add_argument('--config', default=default_config)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    config['_config_path'] = str(Path(args.config).resolve())  # for restarting itself after an update
    app, manager = build_app(config)
    # LAN mode binds all interfaces; the bearer token and allowed_hosts still gate every request.
    uvicorn.run(app, host=config.get('bind_host', '127.0.0.1'), port=config.get('port', 8765), access_log=False)
