"""Private Streamable HTTP MCP bridge to the locally authenticated Codex CLI.

Tasks run through `codex app-server` by default, so their sessions show up in the Codex desktop app.
Set "executor": "exec" in config.json to fall back to headless `codex exec`.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import signal
import time
import uuid
from urllib.parse import quote

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import HTMLResponse, JSONResponse
import uvicorn
import claude_executor


def now():
    return datetime.now(timezone.utc).isoformat()


def local_clock(timestamp):
    """A unix time as local HH:MM, with the date when it is not today."""
    moment = datetime.fromtimestamp(timestamp)
    return moment.strftime('%H:%M' if moment.date() == datetime.now().date() else '%m-%d %H:%M')


# Quota guard: wait this long after the reported reset, at most this long in one go, and re-check this often
# when Codex does not say when the window resets. Module constants, so tests can shrink them.
QUOTA_SLACK_SECONDS, QUOTA_MAX_WAIT_SECONDS, QUOTA_RECHECK_SECONDS, QUOTA_CHECK_TIMEOUT = 60, 6 * 3600, 600, 15
QUOTA_NOTE = '等待 Codex 额度重置：'
QUOTA_WINDOWS = (('primary', '5 小时'), ('secondary', '每周'))


def quota_exhausted(result, threshold):
    """Read an account/rateLimits/read answer: (labels of the windows at or over threshold percent, unix time
    the last of them resets or None). None when nothing is used up or the shape is unknown, so an
    unexpected answer never blocks work."""
    limits = result.get('rateLimits') if isinstance(result, dict) else None
    if not isinstance(limits, dict):
        return None
    number = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool)
    labels, resets = [], []
    for key, label in QUOTA_WINDOWS:
        window = limits.get(key) if isinstance(limits.get(key), dict) else {}
        if number(window.get('usedPercent')) and window['usedPercent'] >= threshold:
            labels.append(label)
            if number(window.get('resetsAt')):
                resets.append(window['resetsAt'])
    return (labels, max(resets) if resets else None) if labels else None


class QuotaExhausted(Exception):
    """The Codex quota is used up; raised before any session is started, so the job can simply wait its turn."""

    def __init__(self, labels, resets_at):
        self.labels, self.resets_at = labels, resets_at
        when = (f'预计 {local_clock(resets_at)}（本地时间）后自动开始' if resets_at else
                f'每 {max(1, round(QUOTA_RECHECK_SECONDS / 60))} 分钟检查一次，恢复后自动开始')
        super().__init__(f"{QUOTA_NOTE}{'和'.join(labels)}额度已用完，{when}")


class AppServerError(RuntimeError):
    def __init__(self, error):
        super().__init__(error.get('message') or json.dumps(error, ensure_ascii=False))
        self.error = error


def task_folder_name(task_id, name=None):
    """A readable folder for a new task: date, name and short id, e.g. 2026-10-12_检查B站登录状态_a0bece4b."""
    label = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', ' ', name or '').strip(' .')[:40].strip()
    parts = [datetime.now().strftime('%Y-%m-%d')] + ([re.sub(r'\s+', '-', label)] if label else []) + [task_id[:8]]
    return '_'.join(parts)


FINISHED = ('completed', 'failed', 'cancelled', 'interrupted', 'timed_out')
WAKE_HEADER = '【自动通知 · 派任务服务】'
WORKER_RULES = '''你是被派出来执行具体任务的子 agent（worker）。必须遵守：
1. 只完成交给你的这件任务。做完后，把结果、遇到的问题和需要人处理的事写进最终回复，由派你来的上级（本机的主agent export）处理。
2. 不要调用本机的派任务服务（codex_tasks / Codex Task Dispatcher，地址如 127.0.0.1:8765 或 :8766/mcp）：不要派新任务，不要取消或修改别的任务，不要修改电脑档案。
3. 不要读取、打印或使用任何访问令牌、密码或 cookie，例如 token.txt、client*.json、.codex/config.toml 里的令牌。
4. 需要别的 agent 帮忙、需要更多信息，或档案需要更新时，在最终回复里说明，由上级安排。
'''
SECRET_FIELD = re.compile(r'pass|secret|token|cookie|key|otp', re.IGNORECASE)
APPROVE_LABEL = re.compile(r'accept|allow|approve|yes|continue|同意|允许|批准|继续', re.IGNORECASE)


def approval_response(method, params):
    """Say yes to a Codex permission prompt; the owner chose full access for dispatched tasks.
    Returns None for anything that cannot be answered without a human, such as secret input."""
    if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
        return {'decision': 'accept'}
    if method in ('applyPatchApproval', 'execCommandApproval'):
        return {'decision': 'approved'}
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


class TaskManager:
    def __init__(self, config):
        self.config = config
        self.state = Path(config['state_dir']).resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        self.jobs_dir = self.state / 'jobs'
        self.jobs_dir.mkdir(exist_ok=True)
        self.root = Path(config['tasks_dir']).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.roots = [Path(p).resolve() for p in config['allowed_roots']]
        self.jobs = {}
        self.processes = {}
        self.queue = asyncio.Queue()
        self.worker = None
        self.closing = False
        # Quota guard: what the queue is waiting for (shown in the profile), whether the orchestrator was
        # told about this pause yet, and a nudge that ends the wait early, e.g. when the waiting job is cancelled.
        self.quota_wait, self.quota_paused, self.quota_wake = None, False, asyncio.Event()
        self.profile_file = self.state / 'profile.json'
        self.profile = json.loads(self.profile_file.read_text(encoding='utf-8')) if self.profile_file.exists() else {}
        self.profile.setdefault('computer', {})
        self.profile.setdefault('agents', {})
        self.notices = {}  # main agent thread -> finished sub-task ids it has not been told about yet
        self._archived_cache = None
        self.notifications_file = self.state / 'notifications.jsonl'
        self.notifications = [json.loads(line) for line in self.notifications_file.read_text(encoding='utf-8').splitlines()
                              if line.strip()] if self.notifications_file.exists() else []
        for file in self.jobs_dir.glob('*/job.json'):
            job = json.loads(file.read_text(encoding='utf-8'))
            self.jobs[job['task_id']] = job
            if job['status'] in ('queued', 'running'):
                job.update(status='interrupted', finished_at=now(), error='Service stopped before completion; submit a new task to retry.')
                self.save(job)

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
        """True once the user archived or deleted this task's session in the Codex app."""
        if job.get('engine') == 'claude':
            return False
        thread = job.get('thread_id')
        if not thread or job['status'] not in FINISHED:
            return False
        active, archived = self.session_files()
        return thread in archived or thread not in active

    def public(self, job, include_result=False):
        fields = ('task_id', 'name', 'engine', 'model', 'effort', 'status', 'workspace', 'created_at', 'started_at', 'finished_at', 'thread_id', 'forked_from_thread_id', 'resume_task_id', 'parent_task_id', 'auto_wake', 'timeout_seconds', 'exit_code', 'error', 'latest_message')
        response = {k: job[k] for k in fields if k in job}
        if self.removed(job):
            response['archived'] = True
        if job.get('thread_id') and job.get('engine') != 'claude':
            response['desktop_url'] = 'codex://threads/' + quote(job['thread_id'], safe='')
            label = (job.get('name') or 'Codex task').replace('[', '(').replace(']', ')').replace('\n', ' ')
            response['desktop_link'] = f"[打开 {label}]({response['desktop_url']})"
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
        return WORKER_RULES

    def is_worker(self, job):
        """Everything except a computer's main agent is a worker. Main agents are recognized by their
        workspace (config main_agent_workspaces) or by being registered with type main."""
        mains = {str(Path(p).resolve()).lower() for p in self.config.get('main_agent_workspaces', [])}
        if str(Path(job['workspace']).resolve()).lower() in mains:
            return False
        return (self.profile['agents'].get(job.get('name') or '') or {}).get('type') != 'main'

    def title_suffix(self, job):
        """Registered resident agents get a marker in their desktop-app title (config resident_title_suffix,
        default " - 固定"; "" turns it off), so they stand out from one-off tasks. Only the title changes."""
        suffix = self.config.get('resident_title_suffix', ' - 固定')
        agent = self.profile['agents'].get(job.get('name') or '') or {}
        return suffix if suffix and agent.get('type') == 'resident' else ''

    def save_profile(self):
        temporary = self.profile_file.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.profile, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.profile_file)

    def update_computer(self, **fields):
        """Hand-written facts about this computer: its role, notes, key software and account login status."""
        computer = self.profile['computer']
        for key, value in fields.items():
            if value is None:
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

    def register_agent(self, name, role, tags=None, agent_type='resident'):
        """Record a long-lived agent on this computer: what it is responsible for and what it can do."""
        name = (name or '').strip()
        if not 1 <= len(name) <= 100 or not (role or '').strip():
            raise ValueError('name and role are required')
        if agent_type not in ('main', 'resident'):
            raise ValueError('agent_type must be main or resident')
        agent = self.profile['agents'].get(name, {'created_at': now()})
        agent.update(role=role.strip(), tags=sorted({t.strip() for t in tags or [] if t and t.strip()}),
                     type=agent_type, updated_at=now())
        self.profile['agents'][name] = agent
        self.save_profile()
        return {'name': name, **agent}

    def remove_agent(self, name):
        if self.profile['agents'].pop(name, None) is None:
            raise ValueError('Unknown agent')
        self.save_profile()
        return {'removed': name}

    async def full_profile(self, refresh=False):
        agents = []
        for name, agent in self.profile['agents'].items():
            jobs = [j for j in self.jobs.values() if j.get('name') == name]
            latest = max(jobs, key=lambda j: j['created_at']) if jobs else None
            agents.append({'name': name, **agent, 'tasks': len(jobs), 'status': latest['status'] if latest else None,
                           'last_active': (latest.get('finished_at') or latest.get('started_at') or latest['created_at']) if latest else None})
        machine = await self.machine_info(refresh)
        return {'computer_name': self.config.get('computer_name') or machine.get('hostname'),
                'machine': machine, 'computer': self.profile['computer'], 'agents': agents, 'quota_wait': self.quota_wait,
                'busy': {'running': [j.get('name') or j['task_id'] for j in self.jobs.values() if j['status'] == 'running'],
                         'queued': sum(j['status'] == 'queued' for j in self.jobs.values())}}

    def open_desktop(self, task_id):
        job = self.get(task_id)
        if job.get('engine') == 'claude':
            raise ValueError('Claude Code sessions have no Codex desktop link; use the dashboard or claude --resume')
        if not job.get('thread_id'):
            raise ValueError('The task has not created a session yet; query get_task again later')
        # Opening a session while a dispatcher turn owns it can contend for its writer lease.
        if any(j['status'] in ('queued', 'running') and j.get('thread_id') == job['thread_id'] for j in self.jobs.values()):
            raise ValueError('Wait for pending work in this session to finish before opening it')
        if os.name != 'nt':
            raise ValueError('Opening the desktop app is currently supported on Windows only; use desktop_url locally')
        uuid.UUID(job['thread_id'])
        response = self.public(job)
        os.startfile(response['desktop_url'])
        return {**response, 'desktop_open_status': 'requested',
                'desktop_open_location': 'server computer',
                'desktop_open_note': 'Requested opening in the Codex desktop app on the server computer. This does not verify that the UI rendered. The desktop app keeps chats it has opened locked until it fully exits, so this dispatcher cannot continue this session until then. For viewing only, use /dashboard instead.'}

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
                last_text = None
                for line in events.read_text(encoding='utf-8', errors='replace').splitlines() if events.exists() else []:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get('type') == 'assistant':
                        for item in event.get('message', {}).get('content', []):
                            if item.get('type') == 'text':
                                last_text = item.get('text', '')
                                entries.append({'kind': 'agent', 'time': job.get('started_at'), 'text': last_text})
                            elif item.get('type') == 'tool_use':
                                entries.append({'kind': 'tool', 'time': job.get('started_at'), 'type': item.get('name'),
                                                'text': json.dumps(item.get('input'), ensure_ascii=False)})
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
                    entries.append({'kind': 'fork', 'time': record.get('timestamp'), 'text': '原会话在桌面 App 里打开过、被占用，以下内容在一个带完整历史的副本里继续。'})
            if record.get('type') != 'event_msg' or payload.get('type') != 'item_completed':
                continue
            item = payload.get('item') or {}
            texts = [c.get('text', '') for c in item.get('content') or [] if isinstance(c, dict)]
            if item.get('type') == 'UserMessage':
                entries.append({'kind': 'user', 'time': record.get('timestamp'), 'text': '\n'.join(texts)})
            elif item.get('type') == 'AgentMessage':
                entries.append({'kind': 'agent', 'time': record.get('timestamp'), 'text': '\n'.join(texts), 'phase': item.get('phase')})
            elif item.get('type') not in ('Reasoning', None):
                detail = {k: item[k] for k in ('server', 'tool', 'command', 'arguments', 'status', 'exitCode') if k in item}
                entries.append({'kind': 'tool', 'time': record.get('timestamp'), 'type': item['type'],
                                'text': json.dumps(detail, ensure_ascii=False)[:2000]})
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
        metadata. Calls without it (the orchestrator, scripts) have no parent unless caller_fallback is on,
        which guesses the single running main-agent turn for clients that don't send metadata."""
        thread = None
        try:
            request = ctx.request_context.request
            task_id = request.headers.get('x-agent-fleet-task') if request else None
            job = self.jobs.get(task_id or '')
            if task_id:
                return task_id if job and job['status'] == 'running' and not self.is_worker(job) else None
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
        return running[0]['task_id'] if len(running) == 1 and self.config.get('caller_fallback') else None

    def after_job(self, job):
        """When a sub-task finishes, queue a message that wakes the main agent which dispatched it."""
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
            line = (f"- 「{child.get('name') or '未命名'}」 task_id={child_id} 状态={child['status']}\n"
                    f"  结果：{text or child.get('error') or '（无）'}")
            # A failure can come after real work (e.g. drafts saved, then "model at capacity"); say how far it got.
            if (child['status'] != 'completed' or not text) and child.get('latest_message'):
                line += f"\n  最后进度：{child['latest_message'][-800:]}"
            lines.append(line)
        prompt = (WAKE_HEADER + '你派出的子任务已结束：\n' + '\n'.join(lines) +
                  '\n\n请根据结果决定下一步：还有工作就继续派任务（派完立即结束本轮）；全部完成就写一份简短汇报作为本轮回复；'
                  '需要人处理就在汇报里说明。这条消息由派任务服务自动发送，不是 orchestrator 的新指令。')
        woken = self.submit(prompt, None, self.config.get('wake_timeout_seconds', 1800), None, resume_task_id=turns[-1]['task_id'])
        self.jobs[woken['task_id']].update(auto_wake=True, wakes_for=children)
        self.save(self.jobs[woken['task_id']])

    def submit(self, prompt, workspace, timeout_seconds, idempotency_key, resume_task_id=None, name=None,
               parent_task_id=None, model=None, effort=None):
        if self.closing:
            raise ValueError('Service is shutting down')
        if not prompt.strip() or len(prompt) > 100_000:
            raise ValueError('prompt must contain 1 to 100,000 characters')
        if name is not None and not 1 <= len(name.strip()) <= 100:
            raise ValueError('name must contain 1 to 100 characters')
        if not 10 <= timeout_seconds <= self.config.get('max_timeout_seconds', 7200):
            raise ValueError('timeout_seconds is outside the configured range')
        if idempotency_key is not None:
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', idempotency_key):
                raise ValueError('Invalid idempotency_key')
            for job in self.jobs.values():
                if job.get('idempotency_key') == idempotency_key:
                    if (job['prompt'], job['requested_workspace'], job['timeout_seconds'], job.get('resume_task_id')) != (prompt, workspace, timeout_seconds, resume_task_id):
                        raise ValueError('idempotency_key was already used for a different request')
                    return self.public(job)
        if sum(j['status'] in ('queued', 'running') for j in self.jobs.values()) >= 32:
            raise ValueError('Task queue is full; retry after a task finishes')
        parent = self.get(resume_task_id) if resume_task_id else None
        engine = 'claude' if self.config.get('executor') == 'claude' else 'codex'
        if parent:
            if parent.get('engine', 'codex') != engine:
                raise ValueError('Cannot resume a session with a different agent engine; create a new session')
            if parent['status'] in ('queued', 'running') or not parent.get('thread_id'):
                raise ValueError('Wait for the task to finish and receive a thread_id before continuing')
            if any(j['status'] in ('queued', 'running') and j.get('thread_id') == parent['thread_id'] for j in self.jobs.values()):
                raise ValueError('This session already has pending work; wait for it to finish')
        task_id = str(uuid.uuid4())
        target = Path(parent['workspace']).resolve() if parent else (
            Path(workspace).resolve() if workspace else self.root / task_folder_name(task_id, name))
        if not any(target.is_relative_to(root) for root in self.roots):
            raise ValueError('workspace is outside allowed_roots in the server configuration')
        target.mkdir(parents=True, exist_ok=True)
        if not target.is_dir():
            raise ValueError('workspace must be a directory')
        job = dict(task_id=task_id, prompt=prompt, requested_workspace=workspace,
                   engine=engine,
                   workspace=str(target), timeout_seconds=timeout_seconds,
                   idempotency_key=idempotency_key, status='queued', created_at=now())
        if parent:
            job.update(resume_task_id=resume_task_id, thread_id=self.latest_thread(parent['thread_id']), name=parent.get('name'))
        elif name is not None:
            job['name'] = name.strip()
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
        self.queue.put_nowait(task_id)
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

    async def cancel(self, task_id):
        job = self.get(task_id)
        if job['status'] in ('queued', 'running'):
            job.update(status='cancelled', finished_at=now())
            self.save(job)
            self.quota_wake.set()  # A job waiting for the Codex quota stops waiting at once.
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
        use_app_server = self.config.get('executor', 'app-server') == 'app-server'
        if not use_app_server:
            self.mark_running(job)  # app-server tasks stay queued until the quota check passes
        folder = self.jobs_dir / job['task_id']
        executable = self.config.get('codex_executable') or shutil.which('codex')
        if not executable:
            raise RuntimeError('Codex CLI executable not found')
        launcher = self.config.get('codex_launcher_executable', executable)
        launcher_args = self.config.get('codex_launcher_args', [])
        if use_app_server:
            # One app-server per task: it creates sessions the desktop app lists, and exiting after the
            # turn releases the session's writer lock so the user can open and continue it there.
            command = [launcher, *launcher_args, 'app-server']
        else:
            command = [launcher, *launcher_args, '--no-daemon', '--ask-for-approval', 'never',
                       '--sandbox', self.config.get('sandbox', 'workspace-write'),
                       '--cd', job['workspace'], 'exec']
            if job.get('resume_task_id'):
                command += ['resume', '--skip-git-repo-check', '--json',
                            '--output-last-message', str(folder / 'result.txt'), job['thread_id'], '-']
            else:
                command += ['--skip-git-repo-check', '--json', '--color', 'never',
                            '--output-last-message', str(folder / 'result.txt'), '-']
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

                async def consume():
                    async for line in process.stdout:
                        event_log.write(line)
                        event_log.flush()
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if event.get('type') == 'thread.started':
                            job['thread_id'] = event.get('thread_id')
                        item = event.get('item') or {}
                        if item.get('type') == 'agent_message':
                            job['latest_message'] = item.get('text', '')[-4000:]
                        if event.get('type') in ('turn.failed', 'error'):
                            job['error'] = str(event.get('error') or event.get('message') or event)[-2000:]
                        self.save(job)
                    return await process.wait()

                if use_app_server:
                    runner = self.drive_app_server(job, process, event_log, folder)
                else:
                    process.stdin.write(job['prompt'].encode('utf-8'))
                    await process.stdin.drain()
                    process.stdin.close()
                    runner = consume()

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
                '. These runtime settings override older engine/permission descriptions in preserved workspace instructions.')
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
            # Optional: file new sessions under a Codex project. projectId is part of app-server's experimental API.
            project = self.config.get('codex_project_id')
            await connection.request('initialize', {'clientInfo': {
                'name': 'codex_task_dispatcher', 'title': 'Codex Task Dispatcher', 'version': '2.0'},
                **({'capabilities': {'experimentalApi': True}} if project else {})})
            await connection.notify('initialized')
            try:
                await self.check_quota(connection, job, log_line)
            except QuotaExhausted:
                quiet = True  # Nothing was started, so let the app-server exit normally.
                raise
            self.mark_running(job)
            suffix = self.title_suffix(job)
            if job.get('resume_task_id'):
                forked = await self.resume_or_fork(connection, job, policy)
                # A resumed resident session gets its marked title even if it was registered later.
                name = job['name'] + suffix if suffix and not forked else None
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
                except AppServerError:
                    pass  # The name is cosmetic; the task can still run.
            turn_params = {'threadId': job['thread_id'], 'input': [{'type': 'text', 'text': job['prompt']}],
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
        blocked = quota_exhausted(result, self.config.get('quota_guard_threshold', 100))
        if blocked:
            raise QuotaExhausted(*blocked)
        if (job.get('latest_message') or '').startswith(QUOTA_NOTE):
            job.pop('latest_message')
            self.save(job)
        if self.quota_paused:
            self.quota_paused = False
            self.notify('Codex 额度已恢复，任务继续',
                        f"排队的任务按原顺序继续运行，先从「{job.get('name') or job['task_id'][:8]}」开始。",
                        task_ids=[job['task_id']], sender='dispatcher')

    async def wait_for_quota(self, job, paused):
        """Hold the whole queue behind this job until the quota should be back. The orchestrator hears about
        the pause once, not on every re-check. Cancelling the job or stopping the service ends the wait at once."""
        if job['status'] != 'queued':
            return
        until = datetime.fromtimestamp(paused.resets_at, timezone.utc).isoformat() if paused.resets_at else None
        self.quota_wait = {'until': until, 'message': str(paused), 'task_id': job['task_id']}
        if not self.quota_paused:
            self.quota_paused = True
            queued = sum(j['status'] == 'queued' for j in self.jobs.values())
            self.notify('Codex 额度用完，任务暂停' + (f'到 {local_clock(paused.resets_at)}' if paused.resets_at else ''),
                        f'{paused}。排队中的 {queued} 个任务会按原顺序等到那时再开始，不需要人处理。',
                        task_ids=[job['task_id']], sender='dispatcher')
        delay = QUOTA_RECHECK_SECONDS if paused.resets_at is None else max(paused.resets_at - time.time(), 0) + QUOTA_SLACK_SECONDS
        deadline = time.monotonic() + min(delay, QUOTA_MAX_WAIT_SECONDS)
        while job['status'] == 'queued' and not self.closing and deadline > time.monotonic():
            self.quota_wake.clear()
            try:
                await asyncio.wait_for(self.quota_wake.wait(), deadline - time.monotonic())
            except asyncio.TimeoutError:
                break

    async def resume_or_fork(self, connection, job, policy):
        """Load an existing session. The desktop app keeps every chat it has opened locked until it fully
        exits; if the session stays held, fail, or fork it when fork_when_busy is on. Returns True if forked."""
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
                if not self.config.get('fork_when_busy', False):
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
                'threadId': job['thread_id'], 'name': f"{base}（续 {datetime.now().strftime('%m-%d %H:%M')}）{self.title_suffix(job)}"})
        except AppServerError:
            pass
        return True

    async def work(self):
        while True:
            task_id = await self.queue.get()
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
                self.quota_wait = None
                self.queue.task_done()
            try:
                self.after_job(job)
            except Exception:
                pass  # A failed wake-up must never stop the queue.

    async def shutdown(self):
        self.closing = True
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        for job in self.jobs.values():
            if job['status'] == 'queued':
                job.update(status='interrupted', finished_at=now(), error='Service shut down before task started')
                self.save(job)


class BearerMiddleware:
    # The dashboard page itself holds no data; it asks for the token and sends it on every API call.
    PUBLIC = {('GET', '/dashboard')}

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
                 'forked_from_thread_id is set, the copy is named "…（续 time）"), and later continue_task calls follow the newest copy.'
                 if config.get('fork_when_busy', False) else
                 'continuing such a session fails with an explanation; tell the human, or start a new task instead.')
    mcp = FastMCP('Codex Task Dispatcher',
        instructions='Submit tasks to Codex running on the server computer. submit_task creates a new session; continue_task(task_id, prompt) reuses an existing dispatched session with its conversation history and workspace. Wait for prior work in the session to finish before continuing. Paths refer to the server computer. Use idempotency_key when retrying submission. Poll get_task every 5–15 seconds. Completed means a final result was produced; inspect failed/timed_out/interrupted errors. Tasks run one at a time. Always include the returned desktop_link as a clickable Markdown link when reporting a task to the human, so they can open it without contacting another agent. desktop_url opens the local Codex app on the computer where clicked. If the human wants the chat shown on the server desktop, call open_task after the task finishes; it opens that chat directly without restarting the app. The user does not need to ask this dispatcher implementation chat to open it. Sessions appear in the Codex desktop app sidebar under their name. The desktop app keeps chats it has opened locked until it fully exits; ' + busy_note + ' A read-only live view of all sessions, which never locks them, is at /dashboard on this server. get_profile describes this computer (hardware, role, software, accounts) and its long-lived agents; keep it current with update_computer_profile and register_agent.',
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=config['allowed_hosts'], allowed_origins=config.get('allowed_origins', [])))

    @mcp.tool()
    async def submit_task(prompt: str, workspace: str | None = None,
                          timeout_seconds: int = 1800, idempotency_key: str | None = None,
                          name: str | None = None, model: str | None = None, effort: str | None = None,
                          ctx: Context = None) -> dict:
        """Dispatch work to Codex; return task_id immediately. Omit workspace for a new isolated folder. An explicit workspace must be an absolute path on the server within allowed_roots. Use a stable unique idempotency_key to avoid duplicate tasks after network retries. name is the session title shown in the Codex desktop app (defaults to the start of the prompt). When a main agent dispatches, it is woken automatically with the result once the task finishes. model / effort: normally OMIT both, so the task uses Codex's default (the latest model at high effort, same as opening a new window); set them only under specific conditions the human asked for."""
        if workspace is not None and not Path(workspace).is_absolute():
            raise ValueError('workspace must be an absolute path on the server')
        return manager.submit(prompt, workspace, timeout_seconds, idempotency_key, name=name,
                              parent_task_id=manager.caller(ctx), model=model, effort=effort)

    @mcp.tool()
    async def continue_task(task_id: str, prompt: str, timeout_seconds: int = 1800,
                            idempotency_key: str | None = None, model: str | None = None, effort: str | None = None,
                            ctx: Context = None) -> dict:
        """Continue the Codex session of a finished dispatched task, preserving its conversation history and workspace. Return a new task_id for this turn. You can reference any finished task in that session; continuation uses its latest saved history. A session the human has opened in the desktop app stays locked until Codex fully exits (see the server instructions for what happens then). Wait until the session has no pending task. Only sessions created by this service can be continued. model / effort: normally OMIT both; set them only when the session keeps failing with "Selected model is at capacity" or the human asks. The override applies to this turn, and later continuations of the session inherit it."""
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
    async def cancel_task(task_id: str) -> dict:
        """Cancel queued or running work and stop its local process tree. Changes already made by a task are preserved."""
        return await manager.cancel(task_id)

    @mcp.tool()
    async def open_task(task_id: str) -> dict:
        """Open a finished task's conversation directly in the Codex desktop app on the SERVER COMPUTER, through the official chat deep link. No app restart is needed. Only this dispatcher's sessions can be opened. Wait for all pending work in the session to finish. Returns requested, not a guarantee that the screen rendered. To let the human open it on the same computer themselves, provide desktop_link from get_task instead."""
        return manager.open_desktop(task_id)

    @mcp.tool()
    async def get_profile(refresh: bool = False) -> dict:
        """This computer's profile: detected hardware (CPU, RAM, GPU/VRAM, disks), network (egress IP, Tailscale), the hand-written role, notes, key software and account login status, the registered long-lived agents with their roles, tags and latest status, and what is running now. Hardware is detected once and saved (see machine.checked_at); pass refresh=true only when it may have changed, e.g. after a hardware or network change."""
        return await manager.full_profile(refresh)

    @mcp.tool()
    async def update_computer_profile(role: str | None = None, notes: str | None = None,
                                      software: list[str] | None = None, accounts: list[dict] | None = None) -> dict:
        """Update the hand-written part of this computer's profile. Only the fields you pass are replaced. role: what this computer is for (e.g. "视频剪辑机，有 GPU"). software: key installed apps with versions if known. accounts: [{"site": "bilibili", "account": "name", "status": "logged_in|logged_out|needs_verification", "expires": "...", "checked_at": "ISO time"}]. Never include passwords, tokens or cookies."""
        return manager.update_computer(role=role, notes=notes, software=software, accounts=accounts)

    @mcp.tool()
    async def register_agent(name: str, role: str, tags: list[str] | None = None, agent_type: str = 'resident') -> dict:
        """Add or update a long-lived agent in this computer's profile. name must match the session name used with submit_task. role: what it is responsible for. tags: capabilities such as gpu, 网页, app, 剪映, bilibili. agent_type: main (this computer's 主agent export) or resident. Do not register one-off tasks."""
        return manager.register_agent(name, role, tags, agent_type)

    @mcp.tool()
    async def notify_orchestrator(title: str, summary: str, needs_human: bool = False,
                                  task_ids: list[str] | None = None, ctx: Context = None) -> dict:
        """Tell the orchestrator (and through it the human) that a goal is finished, has failed for good, or needs a person (login, verification, a decision). title: one short line. summary: the final report, i.e. what was done, results and where they are. needs_human: true if someone must act. Do not use it for intermediate progress."""
        parent = manager.jobs.get(manager.caller(ctx) or '')
        return manager.notify(title, summary, needs_human, task_ids, sender=(parent or {}).get('name'))

    @mcp.tool()
    async def remove_agent(name: str) -> dict:
        """Remove an agent from this computer's profile (its sessions and history are kept)."""
        return manager.remove_agent(name)

    @mcp.custom_route('/health', methods=['GET'])
    async def health(request):
        return JSONResponse({'service': 'codex-task-mcp', 'status': 'ok', 'pid': os.getpid(),
                             'quota_wait_until': (manager.quota_wait or {}).get('until')})

    # Read-only dashboard. Viewing never loads a session into Codex, so it takes no writer lock.
    @mcp.custom_route('/dashboard', methods=['GET'])
    async def dashboard(request):
        page = Path(__file__).with_name('dashboard.html').read_text(encoding='utf-8')
        return HTMLResponse(page, headers={'Cache-Control': 'no-store'})

    @mcp.custom_route('/api/tasks', methods=['GET'])
    async def api_tasks(request):
        jobs = sorted(manager.jobs.values(), key=lambda j: j['created_at'], reverse=True)[:300]
        return JSONResponse({'computer': config.get('computer_name') or os.environ.get('COMPUTERNAME', ''),
                             'tasks': [manager.public(j, include_result=True) for j in jobs]},
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

    app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        # Job workers live for the service lifetime, not one MCP request/session.
        async with mcp.session_manager.run():
            manager.worker = asyncio.create_task(manager.work())
            try:
                yield
            finally:
                await manager.shutdown()

    app.router.lifespan_context = lifespan
    return BearerMiddleware(app, token), manager


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    default_config = os.environ.get('AGENT_FLEET_CONFIG') or str(
        Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.local' / 'share') / 'agent-fleet' / 'config.json')
    parser.add_argument('--config', default=default_config)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    app, manager = build_app(config)
    uvicorn.run(app, host='127.0.0.1', port=config.get('port', 8765), access_log=False)
