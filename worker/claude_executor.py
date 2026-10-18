"""Claude Code print-mode adapter. Uses the owner's CLI sign-in, not an API key."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone


# Claude desktop lists only sessions in its own index, never print-mode (claude -p) sessions. Its
# claude://resume deep link imports one, with its history, into the sidebar. Once the owner opens it there,
# the app keeps its own interactive claude process on that session (importing opens it, so this is usual).
# A background --resume still appends to the same conversation, which the app shows after a restart; only
# while the app is running a turn there (the owner took over) would both write at once, so then we fork.
def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
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


def desktop_holds(session_id, home=None):
    """True while Claude desktop is running a turn in this session (its live process, registered in
    ~/.claude/sessions, reports busy). Merely open and idle does not count."""
    folder = Path(home or Path.home()) / '.claude' / 'sessions'
    try:
        files = list(folder.glob('*.json'))
    except OSError:
        return False
    for path in files:
        try:
            info = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if (info.get('sessionId') == session_id and str(info.get('entrypoint', '')).startswith('claude-desktop')
                and info.get('status') == 'busy' and pid_alive(info.get('pid'))):
            return True
    return False


def show_in_desktop(session_id):
    """Import the session into Claude desktop's sidebar; harmless when the app is not installed."""
    url = 'claude://resume?session=' + session_id
    try:
        if os.name == 'nt':
            os.startfile(url)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def rule_path(path):
    """A path as a Claude permission rule spells it: absolute, forward slashes, /c/... for a Windows drive."""
    path = Path(path).resolve().as_posix()
    if len(path) > 1 and path[1] == ':':
        path = '/' + path[0].lower() + path[2:]
    return path


def session_cwd(manager, job):
    """Claude desktop groups sessions in its sidebar by their exact working directory, not by parent folder.
    So a new session starts in one of the two Agent Fleet folders, by the same rule as Codex: the workspace
    (AgentFleet) for the main agent and every session it opened, the tasks folder (AgentFleet-Other) for
    sessions others opened directly. The task's own folder stays its working folder (--add-dir and the
    instructions). A resumed session keeps the directory it was created in, or --resume would not find it."""
    if job.get('resume_task_id'):
        previous = manager.jobs.get(job['resume_task_id']) or {}
        return previous.get('cwd') or previous.get('workspace') or job['workspace']
    workspace = Path(job['workspace']).resolve()
    tasks = Path(manager.config['tasks_dir']).resolve()
    if workspace != tasks.parent and tasks.parent not in workspace.parents:
        return str(workspace)  # a custom folder outside the Agent Fleet workspace
    return str(tasks.parent if manager.sidebar_side(job) == 'main' else tasks)


def command_for(manager, job, folder):
    config = manager.config
    executable = config.get('claude_executable') or shutil.which('claude')
    if not executable:
        raise RuntimeError('Claude Code executable not found; run worker setup with Engine claude')
    if os.name == 'nt' and Path(executable).suffix.lower() in ('.cmd', '.ps1', '.bat'):
        raise RuntimeError('claude_executable must point to the native claude.exe on Windows')
    command = [executable, *config.get('claude_launcher_args', []), '-p',
               '--output-format', 'stream-json', '--verbose', '--strict-mcp-config',
               '--setting-sources', '', '--no-chrome']
    main = not manager.is_worker(job)
    mcp = {'mcpServers': {}}
    if main:
        token = Path(config['token_file']).read_text(encoding='utf-8').strip()
        mcp['mcpServers']['codex_tasks'] = {
            'type': 'http', 'url': f"http://127.0.0.1:{config.get('port', 8765)}/mcp",
            'headers': {'Authorization': 'Bearer ' + token, 'X-Agent-Fleet-Task': job['task_id']}}
    mcp_path = folder / 'claude-mcp.json'
    mcp_path.write_text(json.dumps(mcp), encoding='utf-8')
    command += ['--mcp-config', str(mcp_path)]
    job['cwd'] = session_cwd(manager, job)
    own = rule_path(job['workspace'])
    command += ['--settings', json.dumps({'permissions': {'deny': ['Read(./.codex/**)', 'Read(./.mcp.json)',
                                                                   f'Read(/{own}/.codex/**)', f'Read(/{own}/.mcp.json)']}})]
    if config.get('sandbox', 'workspace-write') == 'danger-full-access' and config.get('auto_approve'):
        command += ['--dangerously-skip-permissions']
    else:
        # --restricted confines built-in file tools and removes command/code execution.
        # It is a CLI permission boundary, not an OS process sandbox (Windows has no Claude sandbox).
        command += ['--restricted', '--permission-mode', 'dontAsk',
                    '--tools', 'Read,Glob,Grep,Edit,Write', '--allowedTools', f'Edit(/{own}/**)']
        if main:
            command += ['--allowedTools', 'mcp__codex_tasks__*']
    if Path(job['cwd']).resolve() != Path(job['workspace']).resolve():
        command += ['--add-dir', job['workspace']]
    shared = config.get('shared_dir')
    if shared:
        command += ['--add-dir', shared]
        # Explicit permission rule for the shared directory, including Windows drive normalization.
        if '--restricted' in command:
            command += ['--allowedTools', f'Edit(/{rule_path(shared)}/**)']
    if job.get('resume_task_id'):
        command += ['--resume', job['thread_id']]
        if desktop_holds(job['thread_id']):
            # The owner is working in it in Claude desktop right now: continue in a fork, leave theirs untouched.
            job['forked_from_thread_id'] = job['thread_id']
            command += ['--fork-session']
    else:
        job['thread_id'] = str(uuid.uuid4())
        command += ['--session-id', job['thread_id']]
    # The session's title in Claude desktop's sidebar, by the same rule as Codex thread names: the task's name (or
    # the start of its prompt) when it opens; on a resume only when the dispatcher's wanted title changed.
    if job.get('resume_task_id'):
        title = None if job.get('forked_from_thread_id') else manager.changed_title(job)
    else:
        title = (job.get('name') or ' '.join(job['prompt'].split())[:40]) + manager.title_suffix(job)
    if title:
        command += ['--name', title]
        root = manager.session_root(job)
        root['title_set'] = title
        manager.save(root)
    if job.get('model'):
        command += ['--model', job['model']]
    if job.get('effort'):
        if job['effort'] not in ('low', 'medium', 'high', 'max'):
            raise ValueError('Claude Code effort must be low, medium, high or max')
        command += ['--effort', job['effort']]
    if main:
        instructions = Path(job['workspace']) / 'CLAUDE.md'
        if not instructions.is_file():
            instructions = Path(job['workspace']) / 'AGENTS.md'
        if instructions.is_file():
            note = ('\nRuntime facts: this turn runs Claude Code. Permission profile: ' +
                    ('full access' if '--dangerously-skip-permissions' in command else 'restricted file tools; no shell or code execution') +
                    '. Current main agent name: ' + json.dumps(manager.config.get('main_agent_name') or job.get('name'), ensure_ascii=False) +
                    '. These current settings override any older name/engine/permission description in preserved workspace instructions.')
            command += ['--append-system-prompt', instructions.read_text(encoding='utf-8-sig') + note + folder_note(job)]
        elif folder_note(job):
            command += ['--append-system-prompt', folder_note(job).strip()]
    else:
        command += ['--append-system-prompt', manager.worker_rules() + folder_note(job)]
    return command


def folder_note(job):
    if Path(job['cwd']).resolve() == Path(job['workspace']).resolve():
        return ''
    return ('\nYour working folder is ' + json.dumps(str(job['workspace']), ensure_ascii=False) +
            ': create and edit your files there, using absolute paths (the process starts in a parent folder).')


async def run(manager, job):
    folder = manager.jobs_dir / job['task_id']
    command = command_for(manager, job, folder)
    manager.mark_running(job)
    env = os.environ.copy()
    for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'OPENAI_API_KEY', 'CODEX_API_KEY',
                 'CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_SKIP_PROMPT_HISTORY'):
        env.pop(name, None)
    options = {'creationflags': 0x08000000} if os.name == 'nt' else {'start_new_session': True}
    with (folder / 'stderr.log').open('wb') as error_log, (folder / 'events.jsonl').open('wb') as event_log:
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(*command,
            cwd=job.get('cwd') or job['workspace'], stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=error_log, env=env, limit=33_554_432, **options))
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await spawn
            await manager.kill(process)
            raise
        manager.processes[job['task_id']] = process
        result = None

        async def consume():
            nonlocal result
            process.stdin.write(job['prompt'].encode('utf-8'))
            await process.stdin.drain()
            process.stdin.close()
            async for line in process.stdout:
                event_log.write(line)
                event_log.flush()
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if event.get('session_id'):
                    job['thread_id'] = event['session_id']
                if event.get('type') == 'assistant':
                    texts = [c.get('text', '') for c in event.get('message', {}).get('content', []) if c.get('type') == 'text']
                    if texts:
                        job['latest_message'] = '\n'.join(texts)[-4000:]
                if event.get('type') == 'result':
                    result = event
                manager.save(job)
            return await process.wait()

        try:
            if manager.closing or job['status'] != 'running':
                return
            try:
                job['exit_code'] = await asyncio.wait_for(consume(), job['timeout_seconds'])
            except asyncio.TimeoutError:
                await manager.kill(process)
                if job['status'] == 'running':
                    job.update(status='timed_out', error='Task exceeded timeout_seconds')
            if job['status'] == 'running':
                success = (job.get('exit_code') == 0 and result is not None and
                           result.get('subtype') == 'success' and not result.get('is_error'))
                if success:
                    text = result.get('result', '')
                    (folder / 'result.txt').write_text(text, encoding='utf-8')
                    job.update(status='completed', latest_message=text[-4000:])
                    # A new task's session shows up in Claude desktop's sidebar (config claude_desktop_import: off).
                    if (not job.get('resume_task_id') and job.get('thread_id')
                            and manager.config.get('claude_desktop_import', 'new') == 'new'):
                        show_in_desktop(job['thread_id'])
                else:
                    errors = (result or {}).get('errors') or []
                    error_log.flush()
                    detail = '\n'.join(str(e) for e in errors) or (result or {}).get('result')
                    detail = detail or (folder / 'stderr.log').read_text(encoding='utf-8', errors='replace')
                    job.update(status='failed', error=str(detail or 'Claude Code did not produce a successful final result')[-2000:])
        finally:
            await manager.kill(process)
            manager.processes.pop(job['task_id'], None)
            job.setdefault('finished_at', datetime.now(timezone.utc).isoformat())
            manager.save(job)
