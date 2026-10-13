"""Claude Code print-mode adapter. Uses the owner's CLI sign-in, not an API key."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import uuid
from datetime import datetime, timezone


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
    command += ['--settings', json.dumps({'permissions': {'deny': ['Read(./.codex/**)', 'Read(./.mcp.json)']}})]
    if config.get('sandbox', 'workspace-write') == 'danger-full-access' and config.get('auto_approve'):
        command += ['--dangerously-skip-permissions']
    else:
        # --restricted confines built-in file tools and removes command/code execution.
        # It is a CLI permission boundary, not an OS process sandbox (Windows has no Claude sandbox).
        command += ['--restricted', '--permission-mode', 'dontAsk',
                    '--tools', 'Read,Glob,Grep,Edit,Write', '--allowedTools', 'Edit(./**)']
        if main:
            command += ['--allowedTools', 'mcp__codex_tasks__*']
    shared = config.get('shared_dir')
    if shared:
        command += ['--add-dir', shared]
        # Explicit permission rule for the shared directory, including Windows drive normalization.
        path = Path(shared).resolve().as_posix()
        if len(path) > 1 and path[1] == ':':
            path = '/' + path[0].lower() + path[2:]
        if '--restricted' in command:
            command += ['--allowedTools', f'Edit(/{path}/**)']
    if job.get('resume_task_id'):
        command += ['--resume', job['thread_id']]
    else:
        job['thread_id'] = str(uuid.uuid4())
        command += ['--session-id', job['thread_id']]
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
            command += ['--append-system-prompt', instructions.read_text(encoding='utf-8-sig') + note]
    else:
        command += ['--append-system-prompt', manager.worker_rules()]
    return command


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
            cwd=job['workspace'], stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
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
