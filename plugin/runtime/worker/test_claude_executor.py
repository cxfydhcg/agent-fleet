"""Exercise the actual subprocess adapter using a local fake CLI, never a model."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

from server import TaskManager
from claude_executor import command_for


FAKE_CLAUDE = r'''
import json, pathlib, sys, time
sys.stdout.reconfigure(encoding='utf-8')
args=sys.argv[1:]
pathlib.Path(__file__).with_name('calls.json').write_text(json.dumps(args),encoding='utf-8')
prompt=sys.stdin.buffer.read().decode('utf-8')
session=args[args.index('--resume')+1] if '--resume' in args else args[args.index('--session-id')+1]
print(json.dumps({'type':'system','subtype':'init','session_id':session}),flush=True)
if prompt == 'slow': time.sleep(60)
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
                           main_agent_workspaces=[str(self.main)], sandbox='workspace-write')
        self.manager = TaskManager(self.config)
        self.manager.worker = asyncio.create_task(self.manager.work())

    async def asyncTearDown(self):
        await self.manager.shutdown()
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
        self.assertNotIn('desktop_url', public)
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
        with self.assertRaisesRegex(ValueError, 'no Codex desktop link'):
            self.manager.open_desktop(job['task_id'])

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
