"""Exercise the actual subprocess adapter using a local fake CLI, never a model."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import os

from server import TaskManager
import claude_executor
from claude_executor import command_for, desktop_holds


FAKE_CLAUDE = r'''
import json, os, pathlib, sys, time
sys.stdout.reconfigure(encoding='utf-8')
args=sys.argv[1:]
pathlib.Path(__file__).with_name('calls.json').write_text(json.dumps(args),encoding='utf-8')
pathlib.Path(__file__).with_name('cwd.txt').write_text(os.getcwd(),encoding='utf-8')
prompt=sys.stdin.buffer.read().decode('utf-8')
session=args[args.index('--resume')+1] if '--resume' in args else args[args.index('--session-id')+1]
print(json.dumps({'type':'system','subtype':'init','session_id':session}),flush=True)
if prompt == 'slow': time.sleep(60)
if prompt == 'limited':
    print(json.dumps({'type':'rate_limit_event','rate_limit_info':{'status':'rejected','resetsAt':int(time.time())+3600,'rateLimitType':'five_hour'}}),flush=True)
    print(json.dumps({'type':'result','session_id':session,'subtype':'success','is_error':True,'result':"You've hit your limit"}),flush=True)
    sys.exit(1)
print(json.dumps({'type':'assistant','session_id':session,'message':{'content':[{'type':'text','text':prompt}]}}),flush=True)
if prompt != 'no-result':
    result={'type':'result','session_id':session,'subtype':'success','is_error':False,'result':prompt}
    if prompt == 'fail': result.update(subtype='error_during_execution',is_error=True,errors=['fixture failure'])
    print(json.dumps(result),flush=True)
'''


class ClaudeExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.fake = self.root / 'fake.py'
        self.fake.write_text(FAKE_CLAUDE, encoding='utf-8')
        self.token = self.root / 'token.txt'
        self.token.write_text('fixture-token', encoding='utf-8')
        self.main = self.root / 'main-agent'
        self.main.mkdir()
        (self.main / 'CLAUDE.md').write_text('Fixture main-agent instructions', encoding='utf-8')
        self.shared = self.root / 'shared'
        self.shared.mkdir()
        self.config = dict(executor='claude', claude_executable=sys.executable,
                           claude_launcher_args=[str(self.fake)], state_dir=str(self.root / 'state'),
                           tasks_dir=str(self.root / 'tasks'), shared_dir=str(self.shared),
                           allowed_roots=[str(self.root)], token_file=str(self.token),
                           main_agent_workspaces=[str(self.main)], sandbox='workspace-write',
                           # Never read this computer's real Claude transcripts or desktop index.
                           claude_home=str(self.root / 'no-home'), claude_desktop_sessions_dir=str(self.root / 'no-desktop'))
        # Never open the real Claude desktop app from tests; record what would have been shown.
        self.shown, self.held = [], set()
        self.patches = [patch.object(claude_executor, 'show_in_desktop', self.shown.append),
                        patch.object(claude_executor, 'desktop_holds', lambda session, home=None: session in self.held)]
        for p in self.patches:
            p.start()
        self.manager = TaskManager(self.config)
        self.manager.worker = asyncio.create_task(self.manager.work())

    async def asyncTearDown(self):
        await self.manager.shutdown()
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    async def finished(self, task_id):
        async with asyncio.timeout(20):
            while self.manager.get(task_id)['status'] in ('queued', 'running'):
                await asyncio.sleep(.03)
        return self.manager.get(task_id)

    async def test_unicode_result_resume_and_dashboard(self):
        text = '中文 "quotes" $(do-not-run) `literal`'
        first = self.manager.submit(text, None, 10, None, name='fixture')
        job = await self.finished(first['task_id'])
        self.assertEqual(job['status'], 'completed')
        public = self.manager.public(job, True)
        self.assertEqual(public['engine'], 'claude')
        self.assertEqual(public['result'], text)
        self.assertEqual(public['desktop_url'], 'claude://resume?session=' + job['thread_id'])
        self.assertNotIn('archived', public)
        second = self.manager.submit('follow up', None, 10, None, resume_task_id=job['task_id'])
        resumed = await self.finished(second['task_id'])
        self.assertEqual(resumed['status'], 'completed')
        self.assertEqual(resumed['thread_id'], job['thread_id'])
        args = json.loads((self.root / 'calls.json').read_text(encoding='utf-8'))
        self.assertIn('--resume', args)
        transcript = self.manager.transcript(job['thread_id'])
        self.assertEqual([e['text'] for e in transcript if e['kind'] == 'user'], [text, 'follow up'])
        self.assertEqual([e['text'] for e in transcript if e['kind'] == 'agent'], [text, 'follow up'])
        if os.name == 'nt':
            with patch('os.startfile') as start:
                opened = self.manager.open_desktop(job['task_id'])
            start.assert_called_once_with(public['desktop_url'])
            self.assertIn('fork', opened['desktop_open_note'])

    async def test_error_and_missing_result_are_failures(self):
        for prompt in ('fail', 'no-result'):
            task = self.manager.submit(prompt, None, 10, None)
            job = await self.finished(task['task_id'])
            self.assertEqual(job['status'], 'failed')
            self.assertTrue(job['error'])

    async def test_timeout_and_cancellation_release_queue(self):
        task = self.manager.submit('slow', None, 10, None)
        self.manager.get(task['task_id'])['timeout_seconds'] = .2
        job = await self.finished(task['task_id'])
        self.assertEqual(job['status'], 'timed_out')
        slow = self.manager.submit('slow', None, 10, None)
        async with asyncio.timeout(10):
            while slow['task_id'] not in self.manager.processes:
                await asyncio.sleep(.03)
        await self.manager.cancel(slow['task_id'])
        self.assertEqual((await self.finished(slow['task_id']))['status'], 'cancelled')
        last = self.manager.submit('queue recovered', None, 10, None)
        self.assertEqual((await self.finished(last['task_id']))['status'], 'completed')

    async def test_main_mcp_attribution_and_child_wake(self):
        task = self.manager.submit('main', str(self.main), 10, None, name='主agent export')
        job = await self.finished(task['task_id'])
        mcp = json.loads((self.manager.jobs_dir / job['task_id'] / 'claude-mcp.json').read_text())
        headers = mcp['mcpServers']['codex_tasks']['headers']
        self.assertEqual(headers['X-Agent-Fleet-Task'], job['task_id'])
        job['status'] = 'running'
        ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={'x-agent-fleet-task': job['task_id']})))
        self.assertEqual(self.manager.caller(ctx), job['task_id'])
        child = self.manager.submit('child result', None, 10, None, parent_task_id=self.manager.caller(ctx))
        job['status'] = 'completed'
        await self.finished(child['task_id'])
        async with asyncio.timeout(10):
            while not any(j.get('auto_wake') for j in self.manager.jobs.values()):
                await asyncio.sleep(.03)
        wake = next(j for j in self.manager.jobs.values() if j.get('auto_wake'))
        self.assertEqual(wake['thread_id'], job['thread_id'])
        self.assertIn('child result', wake['prompt'])
        # An unrecognized/stale header never falls back to another running parent.
        ctx.request_context.request.headers['x-agent-fleet-task'] = 'other'
        self.assertIsNone(self.manager.caller(ctx))

    async def test_permissions_and_engine_switch(self):
        task = self.manager.submit('inspect', None, 10, None)
        job = await self.finished(task['task_id'])
        args = json.loads((self.root / 'calls.json').read_text())
        self.assertIn('--restricted', args)
        self.assertNotIn('--dangerously-skip-permissions', args)
        self.assertEqual(args[args.index('--tools')+1], 'Read,Glob,Grep,Edit,Write')
        mcp = json.loads((self.manager.jobs_dir / job['task_id'] / 'claude-mcp.json').read_text())
        self.assertEqual(mcp, {'mcpServers': {}})
        self.config.update(sandbox='danger-full-access', auto_approve=True)
        full = command_for(self.manager, job, self.manager.jobs_dir / job['task_id'])
        self.assertIn('--dangerously-skip-permissions', full)
        self.assertNotIn('--restricted', full)
        self.config['executor'] = 'app-server'
        with self.assertRaisesRegex(ValueError, 'different agent engine'):
            self.manager.submit('wrong engine', None, 10, None, resume_task_id=job['task_id'])


if __name__ == '__main__':
    unittest.main()


class SidebarFolderTests(ClaudeExecutorTests):
    """Claude desktop groups by exact working directory, so sessions start in the two Agent Fleet folders."""

    def cwd(self):
        return Path((self.root / 'cwd.txt').read_text(encoding='utf-8')).resolve()

    async def test_main_agent_and_its_sub_tasks_start_in_the_workspace_others_in_the_tasks_folder(self):
        main = await self.finished(self.manager.submit('main turn', str(self.main), 10, None, name='主agent export')['task_id'])
        self.assertEqual(main['status'], 'completed')
        self.assertEqual(self.cwd(), self.root)
        args = json.loads((self.root / 'calls.json').read_text(encoding='utf-8'))
        self.assertIn(str(self.main), args[args.index('--add-dir') + 1])
        self.assertTrue(any('Your working folder is' in a for a in args))
        self.assertEqual(args[args.index('--name') + 1], '主agent export', 'Claude desktop shows the session by this title')
        again = await self.finished(self.manager.submit('again', None, 10, None, resume_task_id=main['task_id'])['task_id'])
        self.assertEqual(again['status'], 'completed')
        self.assertNotIn('--name', json.loads((self.root / 'calls.json').read_text(encoding='utf-8')), 'unchanged title is left alone')
        child = self.manager.submit('child task', None, 10, None, name='child', parent_task_id=main['task_id'])
        await self.finished(child['task_id'])
        self.assertEqual(self.cwd(), self.root, 'a session the main agent opened sits with it')
        sub = await self.finished(self.manager.submit('sub task', None, 10, None, name='helper')['task_id'])
        self.assertEqual(self.cwd(), Path(self.config['tasks_dir']).resolve(), 'opened directly by someone else')
        args = json.loads((self.root / 'calls.json').read_text(encoding='utf-8'))
        own = claude_executor.rule_path(sub['workspace'])
        self.assertIn(f'Edit(/{own}/**)', args, 'restricted sessions may edit only their own folder')
        self.assertNotIn('Edit(./**)', args)

    async def test_resumed_sessions_keep_their_original_directory(self):
        old = await self.finished(self.manager.submit('old', None, 10, None, name='legacy')['task_id'])
        old.pop('cwd')  # a session created before this change ran in its own folder
        await self.finished(self.manager.submit('again', None, 10, None, resume_task_id=old['task_id'])['task_id'])
        self.assertEqual(self.cwd(), Path(old['workspace']).resolve())


class ClaudeParityTests(ClaudeExecutorTests):
    """What Codex workers do, Claude workers do too."""

    async def test_usage_limit_pauses_the_queue_like_a_codex_quota_pause(self):
        task = self.manager.submit('limited', None, 10, None, name='blocked')
        async with asyncio.timeout(5):
            while not self.manager.quota_wait:
                await asyncio.sleep(0.02)
        job = self.manager.get(task['task_id'])
        self.assertEqual(job['status'], 'queued', 'refused before doing anything: back in line, not failed')
        self.assertTrue(job['latest_message'].startswith('等待 Claude 额度重置'))
        self.assertNotIn('finished_at', job)
        self.assertTrue(self.manager.quota_paused)
        await self.manager.cancel(task['task_id'])

    async def test_sessions_archived_or_deleted_in_claude_desktop_count_as_removed(self):
        home, index = self.root / 'home', self.root / 'desktop'
        (home / '.claude' / 'projects' / 'p').mkdir(parents=True)
        (index / 'a' / 'b').mkdir(parents=True)
        self.manager.config.update(claude_home=str(home), claude_desktop_sessions_dir=str(index))
        jobs = {}
        for name in ('kept', 'archived', 'deleted'):
            jobs[name] = await self.finished(self.manager.submit(name, None, 10, None, name=name)['task_id'])
        for name in ('kept', 'archived'):
            (home / '.claude' / 'projects' / 'p' / (jobs[name]['thread_id'] + '.jsonl')).write_text('{}', encoding='utf-8')
        (index / 'a' / 'b' / 'local_x.json').write_text(json.dumps({'cliSessionId': jobs['archived']['thread_id'], 'isArchived': True}), encoding='utf-8')
        self.manager._claude_cache = None
        self.assertEqual({n: self.manager.removed(j) for n, j in jobs.items()}, {'kept': False, 'archived': True, 'deleted': True})
        self.assertIsNone(self.manager.named_session('archived'), 'a removed session is not continued')


class ClaudeDesktopTests(ClaudeExecutorTests):
    async def test_new_sessions_show_in_desktop_and_held_ones_fork(self):
        first = await self.finished(self.manager.submit('hello', None, 10, None, name='fixture')['task_id'])
        self.assertEqual(self.shown, [first['thread_id']], 'a new session is imported into the sidebar once')
        self.held.add(first['thread_id'])  # the owner is working in it in Claude desktop
        second = await self.finished(self.manager.submit('more', None, 10, None, resume_task_id=first['task_id'])['task_id'])
        args = json.loads((self.root / 'calls.json').read_text(encoding='utf-8'))
        self.assertIn('--fork-session', args)
        self.assertEqual(second['forked_from_thread_id'], first['thread_id'])
        self.assertEqual(self.shown, [first['thread_id']], 'continuing never re-imports')
        self.held.clear()
        third = await self.finished(self.manager.submit('again', None, 10, None, resume_task_id=first['task_id'])['task_id'])
        self.assertNotIn('--fork-session', json.loads((self.root / 'calls.json').read_text(encoding='utf-8')))
        self.assertEqual(third['status'], 'completed')

    async def test_import_can_be_turned_off(self):
        self.config['claude_desktop_import'] = 'off'
        await self.finished(self.manager.submit('quiet', None, 10, None, name='fixture')['task_id'])
        self.assertEqual(self.shown, [])

    def test_desktop_holds_reads_live_desktop_sessions(self):
        for p in self.patches:
            p.stop()
        try:
            home = self.root / 'home'
            sessions = home / '.claude' / 'sessions'
            sessions.mkdir(parents=True)
            (sessions / '1.json').write_text(json.dumps({'pid': os.getpid(), 'sessionId': 'open', 'entrypoint': 'claude-desktop', 'status': 'busy'}))
            (sessions / '4.json').write_text(json.dumps({'pid': os.getpid(), 'sessionId': 'idle', 'entrypoint': 'claude-desktop', 'status': 'idle'}))
            (sessions / '2.json').write_text(json.dumps({'pid': os.getpid(), 'sessionId': 'cli', 'entrypoint': 'sdk-cli'}))
            (sessions / '3.json').write_text(json.dumps({'pid': 2 ** 22 + 7, 'sessionId': 'gone', 'entrypoint': 'claude-desktop'}))
            (sessions / 'bad.json').write_text('{')
            self.assertTrue(claude_executor.desktop_holds('open', home))
            self.assertFalse(claude_executor.desktop_holds('cli', home))
            self.assertFalse(claude_executor.desktop_holds('idle', home), 'open but idle keeps the same session')
            self.assertFalse(claude_executor.desktop_holds('gone', home))
            self.assertFalse(claude_executor.desktop_holds('missing', home))
            self.assertFalse(claude_executor.desktop_holds('open', self.root / 'no-home'))
        finally:
            for p in self.patches:
                p.start()

