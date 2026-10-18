"""Lifecycle checks without model requests: python -m unittest test_dispatcher.py."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from server import TaskManager


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config = {'state_dir': str(self.root / 'state'), 'tasks_dir': str(self.root / 'tasks'),
                       'allowed_roots': [str(self.root / 'tasks')], 'codex_executable': sys.executable,
                       'executor': 'app-server', 'writer_wait_seconds': 0}
        self.manager = TaskManager(self.config)
        self.fake = self.root / 'fake.py'
        self.fake.write_text(FAKE_APP_SERVER, encoding='utf-8')
        original = asyncio.create_subprocess_exec

        async def create(*command, **kwargs):
            if command[0] == sys.executable:
                return await original(sys.executable, str(self.fake), *command[1:], **kwargs)
            return await original(*command, **kwargs)

        self.patcher = patch('server.asyncio.create_subprocess_exec', side_effect=create)
        self.patcher.start()
        self.manager.worker = asyncio.create_task(self.manager.work())

    async def asyncTearDown(self):
        await self.manager.shutdown()
        self.patcher.stop()
        self.temp.cleanup()

    async def wait_for(self, task_id, status, timeout=20):
        async with asyncio.timeout(timeout):
            while self.manager.get(task_id)['status'] not in status:
                await asyncio.sleep(0.05)
        return self.manager.get(task_id)

    def test_unsupported_executor_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'executor must be app-server or claude'):
            TaskManager({**self.config, 'executor': 'exec'})

    async def test_submission_retries_and_result_survives_restart(self):
        literal = 'Unicode 中文; $(do-not-execute) `literal` "quotes"'
        first = self.manager.submit(literal, None, 10, 'one')
        second = self.manager.submit(literal, None, 10, 'one')
        self.assertEqual(first['task_id'], second['task_id'])
        with self.assertRaises(ValueError):
            self.manager.submit('different prompt', None, 10, 'one')
        job = await self.wait_for(first['task_id'], {'completed', 'failed'})
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(self.manager.public(job, True)['result'], literal)
        restarted = TaskManager(self.config)
        self.assertEqual(restarted.public(restarted.get(first['task_id']), True)['result'], literal)

    async def test_outside_roots_and_directory_traversal_rejected(self):
        for target in [self.root, self.root / 'tasks' / '..' / 'escape']:
            with self.assertRaises(ValueError):
                self.manager.submit('prompt', str(target), 10, None)

    async def test_task_returns_desktop_link(self):
        first = self.manager.submit('original', None, 10, None, name='Test [name]')
        await self.wait_for(first['task_id'], {'completed'})
        result = self.manager.public(self.manager.get(first['task_id']))
        self.assertEqual(result['desktop_url'], 'codex://threads/as-thread')
        self.assertEqual(result['desktop_link'], '[打开 Test (name)](codex://threads/as-thread)')

    @unittest.skipUnless(os.name == 'nt', 'Windows desktop opening')
    async def test_open_desktop_requires_known_finished_task(self):
        with self.assertRaises(ValueError):
            self.manager.open_desktop('unknown-task')
        task = self.manager.submit('original', None, 10, None)
        with self.assertRaises(ValueError):
            self.manager.open_desktop(task['task_id'])
        job = self.manager.get(task['task_id'])
        job['thread_id'] = '01a12db2-2aa9-7463-a506-72de2859e732'
        with self.assertRaises(ValueError):
            self.manager.open_desktop(task['task_id'])
        await self.manager.cancel(task['task_id'])
        with patch('server.os.startfile') as launch:
            result = self.manager.open_desktop(task['task_id'])
        launch.assert_called_once_with('codex://threads/01a12db2-2aa9-7463-a506-72de2859e732')
        self.assertEqual(result['desktop_open_status'], 'requested')
        self.assertEqual(result['desktop_open_location'], 'server computer')

    async def test_continuation_preserves_session_and_workspace(self):
        first = self.manager.submit('original', None, 10, 'original')
        with self.assertRaises(ValueError):
            self.manager.submit('followup', None, 10, None, resume_task_id=first['task_id'])
        await self.wait_for(first['task_id'], {'completed'})
        followup = self.manager.submit('followup', None, 10, 'followup', resume_task_id=first['task_id'])
        retry = self.manager.submit('followup', None, 10, 'followup', resume_task_id=first['task_id'])
        self.assertEqual(followup['task_id'], retry['task_id'])
        self.assertEqual(first['workspace'], followup['workspace'])
        self.assertEqual(followup['thread_id'], self.manager.get(first['task_id'])['thread_id'])
        with self.assertRaises(ValueError):
            self.manager.submit('another concurrent followup', None, 10, None, resume_task_id=first['task_id'])
        finished = await self.wait_for(followup['task_id'], {'completed', 'failed'})
        self.assertEqual(finished['status'], 'completed')
        events = (self.manager.jobs_dir / followup['task_id'] / 'events.jsonl').read_text()
        self.assertIn('as-thread', events)
        self.assertEqual(self.manager.public(finished, True)['result'], 'followup')

    async def test_continuation_rechecks_revoked_workspace(self):
        first = self.manager.submit('original', None, 10, None)
        await self.wait_for(first['task_id'], {'completed'})
        self.manager.roots = []
        with self.assertRaises(ValueError):
            self.manager.submit('followup', None, 10, None, resume_task_id=first['task_id'])
        with self.assertRaises(ValueError):
            self.manager.submit('unknown task', None, 10, None, resume_task_id='unknown')

    async def test_main_agent_rename_preserves_conversation_and_workspace(self):
        first = self.manager.submit('original', None, 10, None, name='主agent export')
        await self.wait_for(first['task_id'], {'completed'})
        self.config['main_agent_workspaces'] = [first['workspace']]
        self.config['main_agent_name'] = 'main agent windows pc 1'
        followup = self.manager.submit('after rename', None, 10, None, resume_task_id=first['task_id'])
        self.assertEqual(followup['name'], 'main agent windows pc 1')
        self.assertEqual(followup['thread_id'], self.manager.get(first['task_id'])['thread_id'])
        self.assertEqual(followup['workspace'], first['workspace'])
        finished = await self.wait_for(followup['task_id'], {'completed', 'failed'})
        self.assertEqual(finished['status'], 'completed')
        other = self.manager.submit('other agent', None, 10, None, name='Other Agent')
        self.assertEqual(other['name'], 'Other Agent')

    async def test_queued_and_running_cancellation(self):
        first = self.manager.submit('slow', None, 60, None)
        await self.wait_for(first['task_id'], {'running'})
        while first['task_id'] not in self.manager.processes:
            await asyncio.sleep(0.05)
        process = self.manager.processes[first['task_id']]
        second = self.manager.submit('should not run', None, 10, None)
        await self.manager.cancel(second['task_id'])
        await self.manager.cancel(first['task_id'])
        await asyncio.wait_for(self.manager.queue.join(), 10)
        self.assertEqual(self.manager.get(first['task_id'])['status'], 'cancelled')
        self.assertIsNotNone(process.returncode)
        self.assertEqual(self.manager.get(second['task_id'])['status'], 'cancelled')
        self.assertFalse((self.manager.jobs_dir / second['task_id'] / 'result.txt').exists())

    async def test_timeout_and_queue_continues(self):
        first = self.manager.submit('slow', None, 10, None)
        second = self.manager.submit('next', None, 10, None)
        await self.wait_for(first['task_id'], {'timed_out', 'failed'})
        self.assertEqual(self.manager.get(first['task_id'])['status'], 'timed_out')
        await self.wait_for(second['task_id'], {'completed', 'failed'})
        self.assertEqual(self.manager.get(second['task_id'])['status'], 'completed')

    async def test_shutdown_recovery_does_not_repeat_work(self):
        first = self.manager.submit('slow', None, 60, None)
        await self.wait_for(first['task_id'], {'running'})
        second = self.manager.submit('queued', None, 10, None)
        await self.manager.shutdown()
        restarted = TaskManager(self.config)
        self.assertEqual(restarted.get(first['task_id'])['status'], 'interrupted')
        self.assertEqual(restarted.get(second['task_id'])['status'], 'interrupted')
        self.assertEqual(restarted.queue.qsize(), 0)


FAKE_APP_SERVER = r'''import json, pathlib, sys, time
sys.stdin.reconfigure(encoding='utf-8'); sys.stdout.reconfigure(encoding='utf-8')
here = pathlib.Path(__file__).parent
calls = here / 'calls.log'
def log(text):
    with calls.open('a', encoding='utf-8') as f: f.write(text + '\n')
def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + '\n'); sys.stdout.flush()
pending, thread = None, None
for line in sys.stdin:
    message = json.loads(line)
    method, params = message.get('method'), message.get('params') or {}
    if method is None and message.get('id') == 900:
        log('declined' if 'error' in message else 'accepted')
        prompt = pending
        if prompt == 'slow': time.sleep(60)
        if prompt == 'fail':
            send({'method': 'turn/completed', 'params': {'threadId': thread, 'turn': {
                'id': 'turn-1', 'status': 'failed', 'error': {'message': 'boom'}, 'items': []}}})
        else:
            item = {'type': 'agentMessage', 'text': prompt, 'phase': 'final_answer'}
            send({'method': 'item/completed', 'params': {'threadId': thread, 'item': item}})
            send({'method': 'turn/completed', 'params': {'threadId': thread, 'turn': {
                'id': 'turn-1', 'status': 'completed', 'items': [item]}}})
        continue
    if method: log(method + (':' + params['name'] if method == 'thread/name/set' else ''))
    if method in ('thread/start', 'thread/resume', 'turn/start'):
        log('policy:%s:%s:%s' % (method, params.get('approvalPolicy'), params.get('sandbox') or (params.get('sandboxPolicy') or {}).get('type')))
        log('worker-rules:%s:%s' % (method, 'yes' if '子 agent（worker）' in (params.get('developerInstructions') or '') else 'no'))
        if params.get('model') or params.get('effort'): log('model:%s:%s:%s' % (method, params.get('model'), params.get('effort')))
        if params.get('cwd'): log('cwd:%s:%s' % (method, params['cwd']))
        if 'projectId' in params: log('project:%s:%s' % (method, params['projectId']))
        if (params.get('sandboxPolicy') or {}).get('writableRoots'): log('writable:%s' % params['sandboxPolicy']['writableRoots'][0])
    if method == 'initialize':
        if params.get('capabilities'): log('capabilities:%s' % json.dumps(params['capabilities'], sort_keys=True))
        send({'id': message['id'], 'result': {}})
    elif method == 'account/rateLimits/read':
        # A test writes the answer to rate-limits.json ("error" for a JSON-RPC error); the default has quota left.
        file = here / 'rate-limits.json'
        answer = file.read_text(encoding='utf-8') if file.exists() else '{"rateLimits": {"primary": {"usedPercent": 5}}}'
        if answer == 'error': send({'id': message['id'], 'error': {'code': -32601, 'message': 'unknown method'}})
        else: send({'id': message['id'], 'result': json.loads(answer)})
    elif method == 'thread/start': send({'id': message['id'], 'result': {'thread': {'id': 'as-thread'}}})
    elif method == 'thread/name/set': send({'id': message['id'], 'result': {}})
    elif method == 'thread/resume':
        # The "busy" marker simulates the desktop app holding the original session.
        if params['threadId'] == 'as-thread' and (here / 'busy').exists():
            send({'id': message['id'], 'error': {'code': -32600, 'message': 'thread as-thread already has an active writer'}})
        else:
            send({'id': message['id'], 'result': {'thread': {'id': params['threadId']}}})
    elif method == 'thread/fork':
        send({'id': message['id'], 'result': {'thread': {'id': 'as-fork', 'forkedFromId': params['threadId']}}})
    elif method == 'turn/start':
        pending, thread = params['input'][0]['text'], params['threadId']
        send({'id': message['id'], 'result': {'turn': {'id': 'turn-1'}}})
        send({'id': 900, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': thread}})
'''


class AppServerDispatcherTests(DispatcherTests):
    """Same harness, but tasks go through a fake `codex app-server` speaking JSON-RPC on stdio."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.manager.shutdown()
        self.config.update(executor='app-server', writer_wait_seconds=0)
        self.manager = TaskManager(self.config)
        self.fake.write_text(FAKE_APP_SERVER, encoding='utf-8')
        self.calls = self.root / 'calls.log'
        self.manager.worker = asyncio.create_task(self.manager.work())

    def call_log(self):
        return self.calls.read_text(encoding='utf-8').splitlines() if self.calls.exists() else []

    async def test_submission_names_session_and_declines_requests(self):
        first = self.manager.submit('hello 中文 `literal`', None, 10, None, name='主agent export')
        job = await self.wait_for(first['task_id'], {'completed', 'failed'})
        self.assertEqual(job['status'], 'completed', job.get('error'))
        self.assertEqual(job['thread_id'], 'as-thread')
        self.assertEqual(self.manager.public(job, True)['result'], 'hello 中文 `literal`')
        self.assertEqual(self.manager.public(job)['name'], '主agent export')
        self.assertIn('thread/name/set:主agent export', self.call_log())
        self.assertIn('declined', self.call_log())

    async def test_continuation_resumes_before_turn(self):
        first = self.manager.submit('original', None, 10, None)
        await self.wait_for(first['task_id'], {'completed'})
        followup = self.manager.submit('followup', None, 10, None, resume_task_id=first['task_id'])
        job = await self.wait_for(followup['task_id'], {'completed', 'failed'})
        self.assertEqual(job['status'], 'completed', job.get('error'))
        self.assertEqual(self.manager.public(job, True)['result'], 'followup')
        calls = self.call_log()
        self.assertEqual(calls.count('thread/start'), 1)
        self.assertLess(calls.index('thread/resume'), len(calls) - 1 - calls[::-1].index('turn/start'))

    async def test_main_rename_changes_resumed_title_and_runtime_identity(self):
        first = self.manager.submit('original', None, 10, None, name='主agent export')
        await self.wait_for(first['task_id'], {'completed'})
        self.config['main_agent_workspaces'] = [first['workspace']]
        self.config['main_agent_name'] = 'main agent windows pc 1'
        followup = self.manager.submit('after rename', None, 10, None, resume_task_id=first['task_id'])
        job = await self.wait_for(followup['task_id'], {'completed', 'failed'})
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['thread_id'], self.manager.get(first['task_id'])['thread_id'])
        self.assertIn('thread/name/set:main agent windows pc 1', self.call_log())
        result = self.manager.public(job, True)['result']
        self.assertIn('your current main-agent name is "main agent windows pc 1"', result)
        self.assertTrue(result.endswith('after rename'))

    async def test_session_open_elsewhere_is_forked_and_followed(self):
        self.manager.config['fork_when_busy'] = True
        first = self.manager.submit('original', None, 10, None, name='主agent export')
        await self.wait_for(first['task_id'], {'completed'})
        (self.root / 'busy').write_text('', encoding='utf-8')
        followup = self.manager.submit('followup', None, 10, None, resume_task_id=first['task_id'])
        job = await self.wait_for(followup['task_id'], {'completed', 'failed'})
        self.assertEqual(job['status'], 'completed', job.get('error'))
        self.assertEqual((job['thread_id'], job['forked_from_thread_id']), ('as-fork', 'as-thread'))
        self.assertEqual(self.manager.public(job, True)['result'], 'followup')
        self.assertTrue(any(c.startswith('thread/name/set:主agent export（续') for c in self.call_log()))
        # Continuing from the ORIGINAL task id still lands in the newest copy, not a second fork.
        home = self.root / 'codex-home'
        day = home / 'sessions' / '2026' / '10' / '11'
        day.mkdir(parents=True)
        (day / 'rollout-y-as-fork.jsonl').write_text('{}', encoding='utf-8')
        self.manager.config['codex_home'] = str(home)
        again = self.manager.submit('again', None, 10, None, resume_task_id=first['task_id'])
        self.assertEqual(again['thread_id'], 'as-fork')
        job = await self.wait_for(again['task_id'], {'completed', 'failed'})
        self.assertEqual(job['status'], 'completed', job.get('error'))
        self.assertEqual(self.call_log().count('thread/fork'), 1)
        # Once the user archives or deletes the fork in the desktop app, continuing goes back to the original.
        (day / 'rollout-y-as-fork.jsonl').unlink()
        back = self.manager.submit('back', None, 10, None, resume_task_id=again['task_id'])
        self.assertEqual(back['thread_id'], 'as-fork')  # A task id that names the fork itself keeps it.
        self.assertEqual(self.manager.latest_thread('as-thread'), 'as-thread')

    async def test_resident_agents_get_a_title_suffix(self):
        self.manager.register_agent('发布员', '发布视频')
        self.manager.register_agent('主agent export', '总负责', agent_type='main')
        first = self.manager.submit('original', None, 10, None, name='发布员')
        await self.wait_for(first['task_id'], {'completed'})
        followup = self.manager.submit('more', None, 10, None, resume_task_id=first['task_id'])
        job = await self.wait_for(followup['task_id'], {'completed', 'failed'})
        self.assertEqual(job['status'], 'completed', job.get('error'))
        # Set once: a resumed turn leaves the title alone, so a name the user gave it in the app stays.
        self.assertEqual(self.call_log().count('thread/name/set:发布员 - 固定'), 1)
        self.assertEqual(job['name'], '发布员')  # the dispatch name stays plain
        # Main agents and unregistered tasks keep their plain title; an empty suffix turns it off.
        self.calls.unlink()
        main = self.manager.submit('coordinate', None, 10, None, name='主agent export')
        await self.wait_for(main['task_id'], {'completed'})
        again = self.manager.submit('again', None, 10, None, resume_task_id=main['task_id'])
        await self.wait_for(again['task_id'], {'completed'})
        self.manager.config['resident_title_suffix'] = ''
        plain = self.manager.submit('x', None, 10, None, name='发布员')
        await self.wait_for(plain['task_id'], {'completed'})
        names = [c for c in self.call_log() if c.startswith('thread/name/set')]
        self.assertEqual(names, ['thread/name/set:主agent export', 'thread/name/set:发布员'])
        # A fork of a resident session keeps its "（续 …）" title, with the suffix at the end.
        self.manager.config.update(resident_title_suffix=' - 固定', fork_when_busy=True)
        (self.root / 'busy').write_text('', encoding='utf-8')
        forked = self.manager.submit('fork', None, 10, None, resume_task_id=first['task_id'])
        job = await self.wait_for(forked['task_id'], {'completed', 'failed'})
        self.assertEqual(job['status'], 'completed', job.get('error'))
        self.assertTrue(any(c.startswith('thread/name/set:发布员（续') and c.endswith(' - 固定') for c in self.call_log()))

    async def test_title_changes_only_when_the_wanted_title_changes(self):
        first = self.manager.submit('original', None, 10, None, name='剪辑员')
        await self.wait_for(first['task_id'], {'completed'})
        again = self.manager.submit('more', None, 10, None, resume_task_id=first['task_id'])
        await self.wait_for(again['task_id'], {'completed'})
        self.assertEqual([c for c in self.call_log() if c.startswith('thread/name/set')], ['thread/name/set:剪辑员'])
        # Registered as resident later: the next turn marks it once, the turn after leaves it alone.
        self.manager.register_agent('剪辑员', '剪视频')
        for prompt in ('marked', 'left alone'):
            turn = self.manager.submit(prompt, None, 10, None, resume_task_id=first['task_id'])
            await self.wait_for(turn['task_id'], {'completed'})
        self.assertEqual([c for c in self.call_log() if c.startswith('thread/name/set')],
                         ['thread/name/set:剪辑员', 'thread/name/set:剪辑员 - 固定'])

    async def test_english_text(self):
        self.manager.config.update(language='en', codex_app_folders=True)
        import server
        english = server.TaskManager(self.manager.config)
        self.assertEqual(english.title_suffix({'name': 'x'}), '')
        english.register_agent('Publisher', 'publish')
        self.assertEqual(english.title_suffix({'name': 'Publisher'}), ' - resident')
        self.assertEqual(english.app_folders()['other'][0], 'AgentFleet-Other')
        self.assertTrue(english.worker_rules().startswith('You are a sub-agent'))
        self.assertEqual(str(server.QuotaExhausted(['weekly'], None, 'en')),
                         'Waiting for the Codex quota to reset: the weekly quota is used up; checking every 10 minutes, starts automatically once it is back')
        self.assertEqual(server.quota_exhausted({'rateLimits': {'primary': {'usedPercent': 100}}}, 100, 'en'), (['5-hour'], None))
        english.config['main_agent_name'] = 'Main agent'
        english.update_computer(manager_name='Manager Cris')
        note = english.main_agent_note()
        self.assertIn('"Main agent"', note)
        self.assertIn('"Manager Cris"', note)
        self.assertIn('Reply in the language the request is written in', note)
        self.assertIn('use English', note)
        with self.assertRaisesRegex(ValueError, 'orchestrator'):
            english.update_computer('some-main-turn', manager_name='Boss')
        english.update_computer(manager_name='')
        self.assertNotIn('manager_name', english.profile['computer'])

    async def test_session_open_elsewhere_fails_by_default(self):
        first = self.manager.submit('original', None, 10, None)
        await self.wait_for(first['task_id'], {'completed'})
        (self.root / 'busy').write_text('', encoding='utf-8')
        followup = self.manager.submit('followup', None, 10, None, resume_task_id=first['task_id'])
        job = await self.wait_for(followup['task_id'], {'completed', 'failed'})
        self.assertEqual(job['status'], 'failed')
        self.assertIn('held by another Codex window', job['error'])

    async def test_transcript_stitches_forks_and_hides_unknown_sessions(self):
        home = self.root / 'codex-home'
        day = home / 'sessions' / '2026' / '10' / '11'
        day.mkdir(parents=True)
        self.manager.config['codex_home'] = str(home)
        def item(kind, text, ts):
            content = [{'type': 'text', 'text': text}]
            return json.dumps({'timestamp': ts, 'type': 'event_msg', 'payload': {
                'type': 'item_completed', 'item': {'type': kind, 'content': content}}}, ensure_ascii=False)
        (day / 'rollout-x-as-thread.jsonl').write_text('\n'.join([
            json.dumps({'type': 'session_meta', 'payload': {'id': 'as-thread'}}),
            item('UserMessage', '第一问', 't1'), item('AgentMessage', '第一答', 't2'),
            item('UserMessage', '分叉后才写的', 't9')]), encoding='utf-8')
        (day / 'rollout-y-as-fork.jsonl').write_text('\n'.join([
            json.dumps({'type': 'session_meta', 'payload': {'id': 'as-fork', 'forked_from_id': 'as-thread',
                                                            'forked_from_ordinal_exclusive': 3}}),
            item('UserMessage', '第二问', 't3'),
            json.dumps({'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
                'type': 'McpToolCall', 'server': 'codex_tasks', 'tool': 'submit_task', 'status': 'completed'}}})]),
            encoding='utf-8')
        self.manager.jobs['a'] = {'task_id': 'a', 'thread_id': 'as-thread', 'created_at': '1', 'status': 'completed'}
        self.manager.jobs['b'] = {'task_id': 'b', 'thread_id': 'as-fork', 'forked_from_thread_id': 'as-thread',
                                  'created_at': '2', 'status': 'completed'}
        # The original was archived in the desktop app, which moves its file.
        (home / 'archived_sessions').mkdir()
        (day / 'rollout-x-as-thread.jsonl').replace(home / 'archived_sessions' / 'rollout-x-as-thread.jsonl')
        entries = self.manager.transcript('as-fork')
        self.assertEqual([(e['kind'], e.get('text')) for e in entries if e['kind'] != 'tool'],
                         [('user', '第一问'), ('agent', '第一答'), ('fork', entries[2]['text']), ('user', '第二问')])
        self.assertEqual(entries[-1]['kind'], 'tool')
        self.assertIn('submit_task', entries[-1]['text'])
        with self.assertRaises(ValueError):
            self.manager.transcript('someone-elses-chat')

    async def test_full_access_asks_and_auto_approves(self):
        self.manager.config.update(auto_approve=True, sandbox='danger-full-access')
        first = self.manager.submit('open the browser', None, 10, None)
        await self.wait_for(first['task_id'], {'completed'})
        followup = self.manager.submit('again', None, 10, None, resume_task_id=first['task_id'])
        job = await self.wait_for(followup['task_id'], {'completed', 'failed'})
        self.assertEqual(job['status'], 'completed', job.get('error'))
        calls = self.call_log()
        self.assertIn('policy:thread/start:on-request:danger-full-access', calls)
        self.assertIn('policy:thread/resume:on-request:danger-full-access', calls)
        self.assertIn('policy:turn/start:on-request:dangerFullAccess', calls)
        self.assertNotIn('declined', calls)
        self.assertEqual(calls.count('accepted'), 2)

    async def test_workers_get_rules_but_main_agent_does_not(self):
        main_ws = self.root / 'tasks' / 'main-agent-export'
        self.manager.config['main_agent_workspaces'] = [str(main_ws)]
        worker = self.manager.submit('do the work', None, 10, None, name='剪辑视频')
        await self.wait_for(worker['task_id'], {'completed'})
        followup = self.manager.submit('more', None, 10, None, resume_task_id=worker['task_id'])
        await self.wait_for(followup['task_id'], {'completed'})
        self.assertIn('worker-rules:thread/start:yes', self.call_log())
        self.assertIn('worker-rules:thread/resume:yes', self.call_log())
        self.calls.unlink()
        main = self.manager.submit('coordinate', str(main_ws), 10, None, name='主agent export')
        await self.wait_for(main['task_id'], {'completed'})
        self.assertIn('worker-rules:thread/start:no', self.call_log())
        # A main agent registered in the profile is recognized even without the workspace setting.
        self.manager.config['main_agent_workspaces'] = []
        self.manager.register_agent('总管', '主 Agent', agent_type='main')
        self.assertFalse(self.manager.is_worker({'workspace': str(self.root / 'tasks' / 'x'), 'name': '总管'}))
        self.assertTrue(self.manager.is_worker({'workspace': str(self.root / 'tasks' / 'x'), 'name': '剪辑视频'}))

    async def test_task_folders_are_readable_and_moved_sessions_follow_their_folder(self):
        shared = self.root / 'shared'
        self.manager.config['shared_dir'] = str(shared)
        first = self.manager.submit('work', None, 10, None, name='检查B站: 登录?')
        folder = Path(first['workspace']).name
        self.assertRegex(folder, r'^\d{4}-\d{2}-\d{2}_检查B站-登录_[0-9a-f]{8}$')
        await self.wait_for(first['task_id'], {'completed'})
        self.assertIn(f'writable:{shared}', self.call_log())
        # The session's folder is moved (e.g. into a new workspace); continuing tells Codex where it is now.
        moved = self.root / 'tasks' / 'moved-here'
        moved.mkdir(parents=True)
        self.manager.jobs[first['task_id']]['workspace'] = str(moved)
        followup = self.manager.submit('more', None, 10, None, resume_task_id=first['task_id'])
        await self.wait_for(followup['task_id'], {'completed'})
        self.assertIn(f'cwd:thread/resume:{moved}', self.call_log())
        unnamed = self.manager.submit('x', None, 10, None)
        self.assertRegex(Path(unnamed['workspace']).name, r'^\d{4}-\d{2}-\d{2}_[0-9a-f]{8}$')

    async def test_model_and_effort_default_to_codex_and_can_be_set(self):
        plain = self.manager.submit('normal task', None, 10, None)
        await self.wait_for(plain['task_id'], {'completed'})
        self.assertFalse([c for c in self.call_log() if c.startswith('model:')])  # Codex's own default, like a new window
        self.calls.unlink()
        special = self.manager.submit('special task', None, 10, None, model='gpt-6-luna', effort='medium')
        await self.wait_for(special['task_id'], {'completed'})
        followup = self.manager.submit('more', None, 10, None, resume_task_id=special['task_id'])
        await self.wait_for(followup['task_id'], {'completed'})
        calls = self.call_log()
        self.assertIn('model:thread/start:gpt-6-luna:None', calls)
        self.assertEqual(calls.count('model:turn/start:gpt-6-luna:medium'), 2)  # the continuation keeps the choice
        self.assertEqual(self.manager.public(self.manager.get(followup['task_id']))['effort'], 'medium')

    async def test_continue_task_tool_can_switch_model(self):
        # e.g. the session keeps failing with "Selected model is at capacity".
        import server
        created = []

        class Recording(server.FastMCP):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)
        token = self.root / 'token.txt'
        token.write_text('t' * 40, encoding='utf-8')
        await self.manager.shutdown()
        with patch('server.FastMCP', Recording):
            _, self.manager = server.build_app({**self.config, 'token_file': str(token), 'allowed_hosts': []})
        self.manager.worker = asyncio.create_task(self.manager.work())
        first = self.manager.submit('original', None, 10, None)
        await self.wait_for(first['task_id'], {'completed'})
        await created[0].call_tool('continue_task', {'task_id': first['task_id'], 'prompt': 'more',
                                                     'model': 'gpt-6-luna', 'effort': 'low'})
        [followup] = [j for j in self.manager.jobs.values() if j.get('resume_task_id') == first['task_id']]
        self.assertEqual((followup['model'], followup['effort']), ('gpt-6-luna', 'low'))
        await self.wait_for(followup['task_id'], {'completed'})
        self.assertIn('model:turn/start:gpt-6-luna:low', self.call_log())
        later = self.manager.submit('later', None, 10, None, resume_task_id=followup['task_id'])
        self.assertEqual((later['model'], later['effort']), ('gpt-6-luna', 'low'))  # inherited

    async def test_codex_project_is_optional(self):
        plain = self.manager.submit('normal', None, 10, None)
        await self.wait_for(plain['task_id'], {'completed'})
        self.assertFalse([c for c in self.call_log() if c.startswith(('capabilities:', 'project:'))])
        self.calls.unlink()
        self.manager.config['codex_project_id'] = 'proj-123'
        first = self.manager.submit('in project', None, 10, None)
        await self.wait_for(first['task_id'], {'completed'})
        followup = self.manager.submit('more', None, 10, None, resume_task_id=first['task_id'])
        await self.wait_for(followup['task_id'], {'completed'})
        calls = self.call_log()
        self.assertEqual(calls.count('capabilities:{"experimentalApi": true}'), 2)
        self.assertEqual([c for c in calls if c.startswith('project:')], ['project:thread/start:proj-123'])

    async def test_worker_rules_come_from_the_editable_file(self):
        rules = self.root / 'worker-rules.md'
        self.manager.config['worker_rules_file'] = str(rules)
        from server import WORKER_RULES
        self.assertEqual(self.manager.worker_rules(), WORKER_RULES)  # no file yet: built-in text
        rules.write_text('﻿自定义规则：子 agent（worker）只做一件事。', encoding='utf-8')
        self.assertEqual(self.manager.worker_rules(), '自定义规则：子 agent（worker）只做一件事。')
        job = self.manager.submit('do it', None, 10, None, name='剪辑')
        await self.wait_for(job['task_id'], {'completed'})
        self.assertIn('worker-rules:thread/start:yes', self.call_log())  # the edited text reached the task

    async def test_default_policy_stays_locked_down(self):
        first = self.manager.submit('hello', None, 10, None)
        await self.wait_for(first['task_id'], {'completed'})
        self.assertIn('policy:thread/start:never:workspace-write', self.call_log())

    def test_approval_answers(self):
        from server import approval_response
        self.assertEqual(approval_response('item/commandExecution/requestApproval', {}), {'decision': 'accept'})
        self.assertEqual(approval_response('item/fileChange/requestApproval', {}), {'decision': 'accept'})
        self.assertIsNone(approval_response('execCommandApproval', {}))
        self.assertIsNone(approval_response('applyPatchApproval', {}))
        self.assertEqual(approval_response('mcpServer/elicitation/request', {})['action'], 'accept')
        granted = approval_response('item/permissions/requestApproval', {'permissions': {'network': {'enabled': True}}})
        self.assertEqual(granted['permissions'], {'network': {'enabled': True}})
        question = {'id': 'q1', 'header': 'h', 'question': 'Run it?',
                    'options': [{'label': 'Decline'}, {'label': 'Accept'}, {'label': 'Cancel'}]}
        self.assertEqual(approval_response('item/tool/requestUserInput', {'questions': [question]}),
                         {'answers': {'q1': {'answers': ['Accept']}}})
        secret = dict(question, isSecret=True)
        self.assertIsNone(approval_response('item/tool/requestUserInput', {'questions': [secret]}))
        self.assertIsNone(approval_response('account/chatgptAuthTokens/refresh', {}))

    async def test_failed_turn_reports_error(self):
        first = self.manager.submit('fail', None, 10, None)
        job = await self.wait_for(first['task_id'], {'completed', 'failed'})
        self.assertEqual(job['status'], 'failed')
        self.assertIn('boom', job['error'])

    def set_rate_limits(self, answer):
        text = answer if isinstance(answer, str) else json.dumps(answer)
        (self.root / 'rate-limits.json').write_text(text, encoding='utf-8')

    async def test_used_up_quota_pauses_the_whole_queue_in_order(self):
        import server
        resets = int(time.time()) + 3600
        self.set_rate_limits({'rateLimits': {'primary': {'usedPercent': 100, 'windowDurationMins': 300, 'resetsAt': resets},
                                             'secondary': {'usedPercent': 76, 'resetsAt': resets + 86400}}})
        # The reset is an hour away; a tiny cap on one wait makes the dispatcher re-check quickly.
        with patch.object(server, 'QUOTA_MAX_WAIT_SECONDS', 0.2):
            first = self.manager.submit('first', None, 10, None, name='甲')
            second = self.manager.submit('second', None, 10, None, name='乙')
            job = self.manager.get(first['task_id'])
            seen = set()
            async with asyncio.timeout(20):
                while self.call_log().count('account/rateLimits/read') < 3:
                    seen.add((job['status'], 'started_at' in job))
                    await asyncio.sleep(0.01)
            self.assertEqual(seen, {('queued', False)})  # never shown as running during the re-checks
            self.assertEqual(job['latest_message'],
                             f'等待 Codex 额度重置：5 小时额度已用完，预计 {server.local_clock(resets)}（本地时间）后自动开始')
            self.assertEqual(self.manager.get(second['task_id'])['status'], 'queued')
            self.assertFalse([c for c in self.call_log() if c in ('thread/start', 'turn/start', 'thread/resume')])
            self.assertEqual(self.manager.quota_wait['task_id'], first['task_id'])
            [paused] = self.manager.notifications  # once, not on every re-check
            self.assertTrue(paused['title'].startswith('Codex 额度用完，任务暂停到 '))
            self.assertEqual((paused['needs_human'], paused['sender'], paused['task_ids']), (False, 'dispatcher', [first['task_id']]))
            self.set_rate_limits({'rateLimits': {'primary': {'usedPercent': 0}, 'secondary': {'usedPercent': 76}}})
            done = await self.wait_for(second['task_id'], {'completed', 'failed'})
        self.assertEqual(done['status'], 'completed', done.get('error'))
        job = self.manager.get(first['task_id'])
        self.assertEqual((job['status'], job['latest_message']), ('completed', 'first'))
        names = [c for c in self.call_log() if c.startswith('thread/name/set')]
        self.assertEqual(names, ['thread/name/set:甲', 'thread/name/set:乙'])  # the waiting job went first
        self.assertEqual([n['title'] for n in self.manager.notifications][1:], ['Codex 额度已恢复，任务继续'])
        self.assertIsNone(self.manager.quota_wait)

    async def test_waiting_for_quota_can_be_cancelled_and_shut_down(self):
        import server
        self.set_rate_limits({'rateLimits': {'secondary': {'usedPercent': 100, 'resetsAt': int(time.time()) + 6 * 86400}}})
        first = self.manager.submit('first', None, 10, None)
        second = self.manager.submit('second', None, 10, None)
        async with asyncio.timeout(20):
            while self.manager.quota_wait is None:
                await asyncio.sleep(0.05)
        self.assertIn('每周额度已用完', self.manager.get(first['task_id'])['latest_message'])
        async with asyncio.timeout(5):  # the wait (capped at 6 h) ends at once
            await self.manager.cancel(first['task_id'])
            while (self.manager.quota_wait or {}).get('task_id') != second['task_id']:
                await asyncio.sleep(0.05)
        self.assertEqual(self.manager.get(first['task_id'])['status'], 'cancelled')
        self.assertEqual(self.manager.get(second['task_id'])['status'], 'queued')
        self.assertEqual(len(self.manager.notifications), 1)  # still the same pause
        async with asyncio.timeout(5):
            await self.manager.shutdown()
        self.assertEqual(self.manager.get(second['task_id'])['status'], 'interrupted')
        self.assertFalse([c for c in self.call_log() if c in ('thread/start', 'turn/start')])

    async def test_quota_check_errors_and_odd_answers_do_not_block(self):
        for answer in ['error', '{"weird": true}', '[1]', '{"rateLimits": {"primary": {"usedPercent": "lots"}}}',
                       '{"rateLimits": {"primary": null, "secondary": {"usedPercent": 99}}}']:
            self.set_rate_limits(answer)
            task = self.manager.submit('go', None, 10, None)
            job = await self.wait_for(task['task_id'], {'completed', 'failed'})
            self.assertEqual(job['status'], 'completed', (answer, job.get('error')))
            if answer == 'error':
                events = (self.manager.jobs_dir / task['task_id'] / 'events.jsonl').read_text(encoding='utf-8')
                self.assertIn('quota check skipped', events)
        self.assertEqual(self.manager.notifications, [])
        # A lower threshold blocks earlier; the guard can be turned off entirely.
        self.manager.config.update(quota_guard_threshold=90)
        blocked = self.manager.submit('blocked', None, 10, None)
        async with asyncio.timeout(20):
            while self.manager.quota_wait is None:
                await asyncio.sleep(0.05)
        await self.manager.cancel(blocked['task_id'])
        self.calls.unlink()
        self.manager.config.update(quota_guard=False)
        task = self.manager.submit('unguarded', None, 10, None)
        job = await self.wait_for(task['task_id'], {'completed', 'failed'})
        self.assertEqual(job['status'], 'completed', job.get('error'))
        self.assertNotIn('account/rateLimits/read', self.call_log())

    def test_quota_answers_are_read_tolerantly(self):
        from server import quota_exhausted
        limits = lambda **windows: {'rateLimits': windows}
        self.assertIsNone(quota_exhausted(limits(primary={'usedPercent': 99.9}), 100))
        self.assertEqual(quota_exhausted(limits(primary={'usedPercent': 100, 'resetsAt': 5}), 100), (['5 小时'], 5))
        self.assertEqual(quota_exhausted(limits(primary={'usedPercent': 100, 'resetsAt': 5},
                                                secondary={'usedPercent': 100, 'resetsAt': 9}), 100), (['5 小时', '每周'], 9))
        self.assertEqual(quota_exhausted(limits(secondary={'usedPercent': 80}), 80), (['每周'], None))
        for odd in [None, [], {}, {'rateLimits': None}, limits(primary={'usedPercent': True}), limits(primary='full')]:
            self.assertIsNone(quota_exhausted(odd, 100))

    async def test_timeout_and_queue_continues(self):
        await super().test_timeout_and_queue_continues()

    async def test_queued_and_running_cancellation(self):
        await super().test_queued_and_running_cancellation()



class ProfileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = {'state_dir': str(root / 'state'), 'tasks_dir': str(root / 'tasks'),
                       'allowed_roots': [str(root / 'tasks')], 'computer_name': 'pc-test'}
        self.detections = 0

        def fake_detect(config):
            self.detections += 1
            return {'hostname': 'PC', 'cpu': 'Test CPU', 'ram_gb': 32, 'nvidia': [{'name': 'RTX', 'vram_total_mb': '8192'}],
                    'checked_at': datetime.now(timezone.utc).isoformat()}
        self.patcher = patch('server.collect_machine_info', side_effect=fake_detect)
        self.patcher.start()
        self.manager = TaskManager(self.config)

    async def asyncTearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    async def test_profile_combines_detected_and_written_facts(self):
        self.manager.update_computer(role='视频剪辑机', software=['剪映 6.0', 'Chrome'],
                                     accounts=[{'site': 'bilibili', 'account': 'me', 'status': 'logged_in'}])
        self.manager.register_agent('主agent export', '总负责', ['调度'], 'main')
        self.manager.register_agent('B站发布员', '上传视频到 B站', ['网页', 'bilibili', 'bilibili'])
        self.manager.jobs['t1'] = {'task_id': 't1', 'name': 'B站发布员', 'status': 'completed',
                                   'created_at': '2026-10-11T00:00:00', 'finished_at': '2026-10-11T00:01:00'}
        profile = await self.manager.full_profile()
        self.assertEqual(profile['computer_name'], 'pc-test')
        self.assertEqual(profile['machine']['nvidia'][0]['vram_total_mb'], '8192')
        self.assertEqual(profile['computer']['role'], '视频剪辑机')
        agents = {a['name']: a for a in profile['agents']}
        self.assertEqual(agents['B站发布员']['tags'], ['bilibili', '网页'])
        self.assertEqual((agents['B站发布员']['status'], agents['B站发布员']['tasks']), ('completed', 1))
        self.assertEqual(agents['主agent export']['type'], 'main')
        self.assertFalse(profile['permissions']['full_access'])
        self.manager.config.update(sandbox='danger-full-access', auto_approve=True)
        self.assertTrue((await self.manager.full_profile())['permissions']['full_access'])
        # Everything, including the detected hardware, survives a restart; detection runs only on request.
        restarted = TaskManager(self.config)
        self.assertEqual(restarted.profile['computer']['software'], ['剪映 6.0', 'Chrome'])
        await restarted.full_profile()
        await self.manager.full_profile()
        self.assertEqual(self.detections, 1)
        await restarted.full_profile(refresh=True)
        self.assertEqual(self.detections, 2)

    async def test_archived_or_deleted_sessions_are_flagged(self):
        home = Path(self.temp.name) / 'codex-home'
        (home / 'archived_sessions').mkdir(parents=True)
        (home / 'sessions' / '2026' / '10' / '11').mkdir(parents=True)
        archived_id, kept_id, deleted_id = ('01a12db1-be8e-7741-bf8b-7ea3556dc291', '01a12dcc-1a67-7400-b318-99fe00afaa34',
                                            '01a12ddc-6372-7d00-a34f-59a669f29231')
        (home / 'archived_sessions' / f'rollout-2026-10-11T21-19-46-{archived_id}.jsonl').write_text('{}')
        (home / 'sessions' / '2026' / '10' / '11' / f'rollout-2026-10-11T21-48-33-{kept_id}.jsonl').write_text('{}')
        self.manager.config['codex_home'] = str(home)
        job = lambda thread, status='completed': {'task_id': thread[:4], 'thread_id': thread, 'status': status, 'created_at': '1'}
        self.assertTrue(self.manager.public(job(archived_id)).get('archived'))
        self.assertTrue(self.manager.public(job(deleted_id)).get('archived'))
        self.assertNotIn('archived', self.manager.public(job(kept_id)))
        # A running task whose session file isn't written yet is not mistaken for deleted.
        self.assertNotIn('archived', self.manager.public(job(deleted_id, 'running')))

    async def test_profile_rejects_secrets_and_bad_input(self):
        with self.assertRaises(ValueError):
            self.manager.update_computer(accounts=[{'site': 'bilibili', 'password': 'x'}])
        with self.assertRaises(ValueError):
            self.manager.update_computer(accounts=[{'account': 'no site'}])
        with self.assertRaises(ValueError):
            self.manager.register_agent('', 'role')
        with self.assertRaises(ValueError):
            self.manager.register_agent('x', 'role', agent_type='boss')
        with self.assertRaises(ValueError):
            self.manager.remove_agent('nobody')
        self.manager.register_agent('临时', '测试')
        self.manager.remove_agent('临时')
        self.assertNotIn('临时', self.manager.profile['agents'])


class WakeTests(unittest.IsolatedAsyncioTestCase):
    """Callbacks: a finished sub-task wakes the main agent that dispatched it. No worker loop runs here,
    so submitted wake-ups simply stay queued for inspection."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.main_ws = root / 'tasks' / 'main-agent-export'
        self.config = {'state_dir': str(root / 'state'), 'tasks_dir': str(root / 'tasks'),
                       'allowed_roots': [str(root / 'tasks')], 'main_agent_workspaces': [str(self.main_ws)]}
        self.manager = TaskManager(self.config)
        self.main = self.add('main-1', 'P', 'completed', workspace=self.main_ws, name='主agent export')

    async def asyncTearDown(self):
        self.temp.cleanup()

    def add(self, task_id, thread, status, workspace=None, **extra):
        job = {'task_id': task_id, 'thread_id': thread, 'engine': 'codex', 'status': status, 'created_at': f'2026-10-11T00:00:{len(self.manager.jobs):02d}',
               'workspace': str(workspace or Path(self.config['tasks_dir']) / task_id), 'timeout_seconds': 60, **extra}
        Path(job['workspace']).mkdir(parents=True, exist_ok=True)
        self.manager.jobs[task_id] = job
        return job

    def finish(self, task_id, result):
        folder = self.manager.jobs_dir / task_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'result.txt').write_text(result, encoding='utf-8')
        self.manager.jobs[task_id]['status'] = 'completed'
        self.manager.after_job(self.manager.jobs[task_id])

    def queued_wakes(self):
        return [j for j in self.manager.jobs.values() if j.get('auto_wake') and j['status'] == 'queued']

    async def test_caller_comes_from_codex_metadata(self):
        self.main['status'] = 'running'
        class Meta:
            def __init__(self, extra): self.model_extra = extra
        def ctx(extra):
            return type('Ctx', (), {'request_context': type('RC', (), {'meta': Meta(extra)})()})()
        self.assertEqual(self.manager.caller(ctx({'x-codex-turn-metadata': {'thread_id': 'P', 'turn_id': 't'}})), 'main-1')
        self.assertEqual(self.manager.caller(ctx({'x-codex-turn-metadata': json.dumps({'thread_id': 'P'})})), 'main-1')
        self.assertIsNone(self.manager.caller(ctx({'x-codex-turn-metadata': {'thread_id': 'other'}})))
        self.assertIsNone(self.manager.caller(ctx({})))  # e.g. the orchestrator calling directly

    async def test_finished_subtask_wakes_parent_with_result(self):
        self.add('child-1', 'C1', 'running', name='剪辑视频', parent_task_id='main-1')
        self.finish('child-1', '导出完成：D:/out.mp4')
        [wake] = self.queued_wakes()
        self.assertEqual((wake['thread_id'], wake['resume_task_id'], wake['wakes_for']), ('P', 'main-1', ['child-1']))
        self.assertIn('【自动通知', wake['prompt'])
        self.assertIn('「剪辑视频」', wake['prompt'])
        self.assertIn('导出完成：D:/out.mp4', wake['prompt'])
        # Tasks without a parent (e.g. the orchestrator's direct ones) wake nobody.
        self.add('solo', 'S', 'running')
        self.finish('solo', 'x')
        self.assertEqual(len(self.queued_wakes()), 1)

    async def test_failed_subtask_notice_carries_its_last_progress(self):
        self.add('child-1', 'C1', 'running', name='发布', parent_task_id='main-1')
        job = self.manager.jobs['child-1']
        job.update(status='failed', error='Selected model is at capacity', latest_message='x' * 2000 + '已保存 B站和小红书草稿')
        self.manager.after_job(job)
        [wake] = self.queued_wakes()
        self.assertIn('Selected model is at capacity', wake['prompt'])
        self.assertIn('最后进度：', wake['prompt'])
        self.assertIn('已保存 B站和小红书草稿', wake['prompt'])
        self.assertLess(len(wake['prompt']), 1500)  # truncated
        # A normal completion with a result does not repeat the progress line.
        wake['status'] = 'completed'
        self.add('child-2', 'C2', 'running', name='检查', parent_task_id='main-1', latest_message='中间过程')
        self.finish('child-2', '检查完成')
        prompt = self.queued_wakes()[-1]['prompt']
        self.assertIn('检查完成', prompt)
        self.assertNotIn('最后进度', prompt)

    async def test_results_wait_while_parent_is_busy_and_are_batched(self):
        self.add('child-1', 'C1', 'running', name='甲', parent_task_id='main-1')
        self.add('child-2', 'C2', 'running', name='乙', parent_task_id='main-1')
        self.add('busy', 'P', 'queued', workspace=self.main_ws)  # e.g. the orchestrator's message is queued
        self.finish('child-1', 'A 完成')
        self.finish('child-2', 'B 完成')
        self.assertEqual(self.queued_wakes(), [])
        self.manager.jobs['busy']['status'] = 'completed'
        self.manager.after_job(self.manager.jobs['busy'])
        [wake] = self.queued_wakes()
        self.assertEqual(wake['wakes_for'], ['child-1', 'child-2'])
        self.assertIn('A 完成', wake['prompt'])
        self.assertIn('B 完成', wake['prompt'])

    async def test_notifications_are_numbered_and_survive_restart(self):
        first = self.manager.notify('两步网页检查完成', '标题分别是 A 和 B', task_ids=['t1'], sender='主agent export')
        second = self.manager.notify('需要登录小红书', '请扫码登录', needs_human=True)
        self.assertEqual((first['id'], second['id']), (1, 2))
        restarted = TaskManager(self.config)
        self.assertEqual([n['title'] for n in restarted.notifications], ['两步网页检查完成', '需要登录小红书'])
        self.assertTrue(restarted.notifications[1]['needs_human'])
        self.assertEqual(restarted.notify('再一条', '内容')['id'], 3)
        for bad in [('', 'x'), ('x', ''), ('x' * 201, 'y')]:
            with self.assertRaises(ValueError):
                self.manager.notify(*bad)

    async def test_back_to_back_wakes_are_capped(self):
        self.manager.config['max_auto_wakes'] = 2
        for i in range(2):
            self.add(f'wake-{i}', 'P', 'completed', workspace=self.main_ws, auto_wake=True)
        self.add('child-1', 'C1', 'running', name='甲', parent_task_id='main-1')
        self.finish('child-1', 'done')
        self.assertEqual(self.queued_wakes(), [])
        self.assertIn('paused', self.manager.jobs['wake-1']['latest_message'])



class SessionControlTests(unittest.IsolatedAsyncioTestCase):
    """Who may open and control sessions: a main agent only its own ones, the orchestrator (no caller) all."""
    asyncSetUp, asyncTearDown, add, finish, queued_wakes = (WakeTests.asyncSetUp, WakeTests.asyncTearDown,
                                                            WakeTests.add, WakeTests.finish, WakeTests.queued_wakes)

    def child(self, name, caller='main-1'):
        job = self.manager.jobs[self.manager.submit('work', None, 60, None, name=name, parent_task_id=caller)['task_id']]
        job.update(status='completed', thread_id='T-' + job['task_id'])
        return job

    async def test_main_agent_controls_only_sessions_it_opened(self):
        self.main['status'] = 'running'
        mine = self.child('剪辑')
        theirs = self.add('orch', 'O', 'completed', name='orchestrator 开的')
        self.assertEqual(self.manager.public(mine)['owner'], '主agent export')
        self.assertNotIn('owner', self.manager.public(theirs))
        for attempt in (lambda: self.manager.submit('more', None, 60, None, resume_task_id='orch', parent_task_id='main-1'),
                        lambda: self.manager.open_desktop('orch', 'main-1')):
            with self.assertRaisesRegex(ValueError, 'did not open this session'):
                attempt()
        with self.assertRaisesRegex(ValueError, 'did not open this session'):
            await self.manager.cancel('orch', 'main-1')
        # A later turn of the same main agent still owns it, through the whole chain of continuations.
        self.main['status'] = 'completed'
        later = self.add('main-2', 'P', 'running', workspace=self.main_ws, name='主agent export', resume_task_id='main-1')
        step = self.manager.submit('more', None, 60, None, resume_task_id=mine['task_id'], parent_task_id='main-2')
        self.assertEqual(step['owner'], '主agent export')
        self.assertEqual((await self.manager.cancel(step['task_id'], 'main-2'))['status'], 'cancelled')
        # The orchestrator keeps full control, and internal wake-ups are not limited.
        self.manager.submit('more', None, 60, None, resume_task_id='orch')
        later['status'] = 'completed'
        self.add('child-x', 'CX', 'running', name='甲', parent_task_id='main-2')
        self.finish('child-x', 'done')
        self.assertEqual(len(self.queued_wakes()), 1)

    async def test_can_dispatch_and_locked_are_set_by_the_orchestrator_only(self):
        self.main['status'] = 'running'
        owned = self.child('发布员')
        self.manager.register_agent('发布员', '发布视频', locked=True)
        self.manager.register_agent('主agent export', '总负责', agent_type='main', can_dispatch=False)
        with self.assertRaisesRegex(ValueError, 'can_dispatch is off'):
            self.manager.submit('new', None, 60, None, name='剪辑', parent_task_id='main-1')
        self.manager.submit('new', None, 60, None, name='剪辑')  # the orchestrator is not limited
        with self.assertRaisesRegex(ValueError, 'locked'):
            self.manager.submit('more', None, 60, None, resume_task_id=owned['task_id'], parent_task_id='main-1')
        with self.assertRaisesRegex(ValueError, 'locked'):
            await self.manager.cancel(owned['task_id'], 'main-1')
        self.manager.submit('more', None, 60, None, resume_task_id=owned['task_id'])
        # The main agent can neither lift its own limit nor touch a locked agent's entry.
        for attempt in (lambda: self.manager.register_agent('主agent export', '总负责', agent_type='main', can_dispatch=True, caller='main-1'),
                        lambda: self.manager.register_agent('主agent export', '总负责', agent_type='resident', caller='main-1'),
                        lambda: self.manager.remove_agent('主agent export', caller='main-1'),
                        lambda: self.manager.register_agent('发布员', '别的', caller='main-1'),
                        lambda: self.manager.remove_agent('发布员', caller='main-1')):
            with self.assertRaises(ValueError):
                attempt()
        self.manager.register_agent('主agent export', '总负责（新）', agent_type='main', caller='main-1')  # plain updates still work
        self.assertIs(self.manager.agent_policy('主agent export')['can_dispatch'], False)
        self.manager.register_agent('发布员', '发布视频和封面')  # omitted flags are kept
        self.assertIs(self.manager.agent_policy('发布员')['locked'], True)
        # Even when allowed to dispatch, it cannot open a new session under a locked agent's name.
        self.manager.register_agent('主agent export', '总负责', agent_type='main', can_dispatch=True)
        with self.assertRaisesRegex(ValueError, 'locked agent'):
            self.manager.submit('new', None, 60, None, name='发布员', parent_task_id='main-1')
        self.manager.submit('new', None, 60, None, name='剪辑', parent_task_id='main-1')

    async def test_tools_pass_the_caller(self):
        import server
        from mcp.server.fastmcp.exceptions import ToolError
        created = []

        class Recording(server.FastMCP):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)
        token = Path(self.temp.name) / 'token.txt'
        token.write_text('t' * 40, encoding='utf-8')
        with patch('server.FastMCP', Recording):
            _, self.manager = server.build_app({**self.config, 'token_file': str(token), 'allowed_hosts': []})
        self.main = self.add('main-1', 'P', 'running', workspace=self.main_ws, name='主agent export')
        self.add('orch', 'O', 'completed', name='别人的')
        self.manager.register_agent('主agent export', '总负责', agent_type='main', can_dispatch=False)
        calls = [('submit_task', {'prompt': 'x'}), ('continue_task', {'task_id': 'orch', 'prompt': 'x'}),
                 ('cancel_task', {'task_id': 'orch'}), ('open_task', {'task_id': 'orch'}),
                 ('register_agent', {'name': '主agent export', 'role': 'r', 'agent_type': 'main', 'can_dispatch': True}),
                 ('remove_agent', {'name': '主agent export'})]
        with patch.object(server.TaskManager, 'caller', return_value='main-1'):
            for tool, arguments in calls:
                with self.assertRaises(ToolError, msg=tool):
                    await created[0].call_tool(tool, arguments)
        await created[0].call_tool('register_agent', {'name': '主agent export', 'role': 'r', 'agent_type': 'main', 'can_dispatch': True})
        self.assertIs(self.manager.agent_policy('主agent export')['can_dispatch'], True)

    async def test_registered_agent_keeps_a_single_session(self):
        archived = set()
        self.manager.removed = lambda job: job.get('thread_id') in archived
        self.main['status'] = 'running'
        first = self.child('发布员')
        self.manager.register_agent('发布员', '发布视频')
        for caller in ('main-1', None):
            with self.assertRaisesRegex(ValueError, f"task_id={first['task_id']}"):
                self.manager.submit('again', None, 60, None, name='发布员', parent_task_id=caller)
        # The main agent finds the session through its profile and continues it instead.
        [agent] = [a for a in (await self.manager.full_profile())['agents'] if a['name'] == '发布员']
        self.assertEqual((agent['task_id'], agent['sessions']), (first['task_id'], 1))
        step = self.manager.submit('more', None, 60, None, resume_task_id=agent['task_id'], parent_task_id='main-1')
        with self.assertRaisesRegex(ValueError, step['task_id']):  # a queued turn counts too
            self.manager.submit('again', None, 60, None, name='发布员')
        # One-off names may repeat, and once the user archives the session a fresh one may start.
        self.child('剪辑'), self.child('剪辑')
        self.manager.jobs[step['task_id']]['status'] = 'cancelled'
        archived.add(first['thread_id'])
        self.child('发布员')

    async def test_main_agent_name_cannot_be_reused_for_another_session(self):
        self.manager.removed = lambda job: False
        self.manager.register_agent('主agent export', '总负责', agent_type='main')
        with self.assertRaisesRegex(ValueError, 'main-1'):
            self.manager.submit('pretend', None, 60, None, name='主agent export')

    async def test_main_agent_is_found_by_its_permanent_id_whatever_its_names(self):
        self.manager.removed = lambda job: False
        main_id = self.manager.profile['main_agent_id']
        self.assertTrue(main_id)
        self.assertEqual(self.manager.main_agent()['task_id'], 'main-1')
        # Renamed by config, given a management name, a newer turn: still the same id, pointing at the newest turn.
        self.manager.config['main_agent_name'] = '经理'
        self.manager.update_computer(manager_name='经理cris')
        self.add('main-2', 'P', 'completed', workspace=self.main_ws, name='经理', resume_task_id='main-1')
        self.child('剪辑')
        found = self.manager.main_agent()
        self.assertEqual((found['id'], found['name'], found['manager_name'], found['task_id']), (main_id, '经理', '经理cris', 'main-2'))
        # The id survives a restart and a config that names another one (the service's own record wins).
        again = TaskManager({**self.config, 'main_agent_id': 'from-config'})
        self.assertEqual(again.profile['main_agent_id'], main_id)
        fresh = Path(self.temp.name) / 'fresh'
        self.assertEqual(TaskManager({**self.config, 'state_dir': str(fresh), 'main_agent_id': 'from-config'}).profile['main_agent_id'], 'from-config')

    async def test_header_identifies_the_caller_even_after_a_profile_change(self):
        from types import SimpleNamespace
        self.main['status'] = 'running'
        self.manager.config['main_agent_workspaces'] = []
        self.manager.register_agent('主agent export', '总负责', agent_type='resident')
        ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={'x-agent-fleet-task': 'main-1'})))
        self.assertEqual(self.manager.caller(ctx), 'main-1')



class AppSidebarFolderTests(unittest.IsolatedAsyncioTestCase):
    """codex_app_folders: the dispatcher files its sessions into two desktop-app folders while the app is closed."""
    asyncSetUp_base, asyncTearDown, add = WakeTests.asyncSetUp, WakeTests.asyncTearDown, WakeTests.add

    async def asyncSetUp(self):
        await self.asyncSetUp_base()
        self.codex = Path(self.temp.name) / 'codex'
        self.codex.mkdir()
        self.state = self.codex / '.codex-global-state.json'
        self.state.write_text(json.dumps({'local-projects': {'p-user': {'id': 'p-user', 'name': 'mine', 'rootPaths': ['C:\\mine']}},
                                          'thread-project-assignments': {'U': {'projectKind': 'local', 'projectId': 'p-user'}}}), encoding='utf-8')
        self.manager.config.update(codex_home=str(self.codex), codex_app_folders=True, codex_app_folder_interval=0.01)
        self.add('child', 'C', 'completed', name='剪辑', parent_task_id='main-1')
        self.add('child-2', 'C2', 'completed', resume_task_id='child', forked_from_thread_id='C')
        self.add('direct', 'D', 'completed', name='发布员')
        self.add('user-filed', 'U', 'completed')

    def folders(self):
        state = json.loads(self.state.read_text(encoding='utf-8'))
        names = {p['id']: p['name'] for p in state['local-projects'].values()}
        return {t: names[a['projectId']] for t, a in state['thread-project-assignments'].items()}

    async def test_app_reopening_before_write_keeps_state_untouched(self):
        original = self.state.read_bytes()
        with patch('server.desktop_app_running', side_effect=[[], ['Codex']]):
            self.assertEqual(self.manager.file_into_app_folders(self.manager.app_folders(), self.manager.folder_threads()), 'waiting')
        self.assertEqual(self.state.read_bytes(), original)

    async def test_sessions_are_filed_by_who_opened_them_once_the_app_is_closed(self):
        folders = self.manager.app_folders()
        with patch('server.desktop_app_running', return_value=['ChatGPT.exe']):
            self.assertEqual(self.manager.file_into_app_folders(folders, self.manager.folder_threads()), 'waiting')
        self.assertEqual(self.folders(), {'U': 'mine'})
        with patch('server.desktop_app_running', return_value=[]):
            self.assertEqual(self.manager.file_into_app_folders(folders, self.manager.folder_threads()), 'filed')
        self.assertEqual(self.folders(), {'P': 'AgentFleet', 'C': 'AgentFleet', 'C2': 'AgentFleet',
                                          'D': 'AgentFleet-其他', 'U': 'mine'})
        self.assertTrue(self.state.with_name(self.state.name + '.bak-agent-fleet').exists())
        # Custom names; the folders are found again by their root, so nothing is duplicated.
        self.manager.config['codex_app_folders'] = {'main': '主 agent 派的', 'other': '其他'}
        with patch('server.desktop_app_running', return_value=[]):
            self.manager.file_into_app_folders(self.manager.app_folders(), self.manager.folder_threads())
        self.assertEqual(len(json.loads(self.state.read_text(encoding='utf-8'))['local-projects']), 3)
        self.manager.config['executor'] = 'claude'
        self.assertIsNone(self.manager.app_folders())

    async def test_loop_waits_for_the_app_and_retries_after_tasks(self):
        running = ['ChatGPT.exe']
        with patch('server.desktop_app_running', side_effect=lambda: running):
            self.manager.folder_task = asyncio.create_task(self.manager.folder_loop())
            async with asyncio.timeout(5):
                while not self.manager.folders_status:
                    await asyncio.sleep(0.01)
                self.assertEqual((self.manager.folders_status['result'], self.manager.folders_pending), ('waiting', True))
                running.clear()
                while self.manager.folders_pending:
                    await asyncio.sleep(0.01)
            self.assertEqual(self.folders()['D'], 'AgentFleet-其他')
            self.add('later', 'L', 'completed', parent_task_id='main-1')
            self.manager.after_job(self.manager.jobs['later'])
            async with asyncio.timeout(5):
                while 'L' not in self.folders():
                    await asyncio.sleep(0.01)
        self.assertEqual(self.folders()['L'], 'AgentFleet')
        self.manager.closing = True
        self.manager.folder_task.cancel()



class PairingTests(unittest.TestCase):
    """An orchestrator gets the connection info for a one-time code, without anyone copying connection.json."""

    def setUp(self):
        import server
        from starlette.testclient import TestClient
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / 'token.txt').write_text('s' * 40, encoding='utf-8')
        config = {'state_dir': str(root / 'data' / 'state'), 'tasks_dir': str(root / 'ws' / 'tasks'), 'allowed_roots': [str(root / 'ws')],
                  'token_file': str(root / 'token.txt'), 'allowed_hosts': [], 'computer_name': 'win-pc', 'port': 8765,
                  'lan_url': 'http://192.168.1.23:8765/mcp', 'main_agent_name': '经理', 'main_agent_workspaces': [str(root / 'ws' / 'main-agent')]}
        app, self.manager = server.build_app(config)
        self.client = TestClient(app)
        self.auth = {'Authorization': 'Bearer ' + 's' * 40}

    def tearDown(self):
        self.temp.cleanup()

    def test_code_gives_the_connection_once(self):
        self.assertEqual(self.client.get('/pair/info').status_code, 404)  # nothing to find until a window is opened
        self.assertEqual(self.client.post('/pair/start').status_code, 401)  # opening one needs the token
        started = self.client.post('/pair/start', headers=self.auth).json()
        self.assertRegex(started['code'], r'^\d{4}-\d{4}$')
        info = self.client.get('/pair/info').json()
        self.assertEqual((info['computer_name'], info['main_agent_name'], info['lan_url']), ('win-pc', '经理', 'http://192.168.1.23:8765/mcp'))
        self.assertNotIn('token', json.dumps(info))
        self.assertEqual(self.client.post('/pair/claim', json={'code': '0000-0000'}).status_code, 403)
        claimed = self.client.post('/pair/claim', json={'code': started['code'].replace('-', '')}).json()
        self.assertEqual((claimed['name'], claimed['token'], claimed['main_agent_id']), ('win-pc', 's' * 40, self.manager.profile['main_agent_id']))
        self.assertEqual(self.client.post('/pair/claim', json={'code': started['code']}).status_code, 404)  # used once
        self.assertEqual(self.client.get('/pair/info').status_code, 404)

    def test_wrong_codes_and_time_close_the_window(self):
        import server
        code = self.client.post('/pair/start', headers=self.auth).json()['code']
        for attempt in range(server.PAIRING_ATTEMPTS):
            answer = self.client.post('/pair/claim', json={'code': '1111-1111' if code != '1111-1111' else '2222-2222'})
            self.assertEqual(answer.status_code, 403)
        self.assertFalse(answer.json()['window_open'])
        self.assertEqual(self.client.post('/pair/claim', json={'code': code}).status_code, 404)
        code = self.client.post('/pair/start', headers=self.auth).json()['code']
        self.manager.pairing['expires'] = time.monotonic() - 1
        self.assertEqual(self.client.post('/pair/claim', json={'code': code}).status_code, 404)


class CodexAppFolderTests(unittest.TestCase):
    """The JSON transform of tools/codex_app_folder.py, without touching real files or processes."""

    def test_creates_folder_and_assigns_only_unfiled_threads(self):
        from tools.codex_app_folder import add_to_folder
        state = {'other-key': 1, 'local-projects': {'p1': {'id': 'p1', 'name': 'memora', 'rootPaths': ['C:\\memora']}},
                 'project-order': ['p1'], 'thread-project-assignments': {'t-user': {'projectKind': 'local', 'projectId': 'p1'}},
                 'projectless-thread-ids': ['t1', 't-other']}
        updated, report = add_to_folder(state, 'AgentFleet', 'C:\\AgentFleet', ['t1', 't2', 't-user', 't1'], 123, 'new-id')
        self.assertEqual(state['project-order'], ['p1'])  # the input is not modified
        self.assertEqual(updated['local-projects']['new-id'], {'id': 'new-id', 'name': 'AgentFleet',
                         'rootPaths': ['C:\\AgentFleet'], 'createdAt': 123, 'updatedAt': 123})
        self.assertEqual(updated['project-order'], ['new-id', 'p1'])
        self.assertEqual(updated['thread-project-assignments']['t2'], {'projectKind': 'local', 'projectId': 'new-id'})
        self.assertEqual(updated['thread-project-assignments']['t-user']['projectId'], 'p1')  # the user's choice stays
        self.assertEqual(updated['projectless-thread-ids'], ['t-other'])
        self.assertEqual(updated['other-key'], 1)
        self.assertEqual((report['created'], report['assigned'], report['already_assigned']), (True, ['t1', 't2'], 1))
        # A second run reuses the folder that has this root and has nothing left to do.
        again, report = add_to_folder(updated, 'AgentFleet', 'C:\\AgentFleet\\', ['t1', 't3'], 456, 'other-id')
        self.assertEqual((report['project_id'], report['created'], report['assigned']), ('new-id', False, ['t3']))
        self.assertEqual(again['project-order'], ['new-id', 'p1'])

    def test_empty_state_gets_the_keys(self):
        from tools.codex_app_folder import add_to_folder
        updated, report = add_to_folder({}, 'AgentFleet', '/home/u/AgentFleet', ['t1'], 1, 'id')
        self.assertEqual((updated['project-order'], updated['projectless-thread-ids']), (['id'], []))
        self.assertEqual(report['assigned'], ['t1'])



class OutputFileTests(unittest.TestCase):
    """The read-only file API: only shared/tasks are visible, and ranges let videos be scrubbed."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.shared, self.tasks, self.private = root / 'shared', root / 'tasks', root / 'private'
        for d in (self.shared / 'ep5', self.tasks / 't1', self.private):
            d.mkdir(parents=True)
        (self.shared / 'ep5' / 'cover.png').write_bytes(b'png')
        (self.shared / 'clip.mp4').write_bytes(bytes(range(100)))
        (self.private / 'token.txt').write_text('secret')
        self.config = {'shared_dir': str(self.shared), 'tasks_dir': str(self.tasks)}

    def tearDown(self):
        self.temp.cleanup()

    def test_paths_stay_inside_shared_and_tasks(self):
        from server import resolve_output_path
        self.assertEqual(resolve_output_path(self.config, 'ep5/cover.png'), (self.shared / 'ep5' / 'cover.png').resolve())
        self.assertEqual(resolve_output_path(self.config, str(self.tasks / 't1')), (self.tasks / 't1').resolve())
        for bad in ('../private/token.txt', str(self.private / 'token.txt'), str(self.shared.parent)):
            with self.assertRaises(ValueError):
                resolve_output_path(self.config, bad)
        with self.assertRaises(ValueError):
            resolve_output_path({}, 'anything')

    def test_listing_shows_kinds_and_parent(self):
        from server import list_output_dir
        top = list_output_dir(self.config, '')
        self.assertEqual([e['name'] for e in top['entries']][0], 'ep5')  # folders first
        clip = next(e for e in top['entries'] if e['name'] == 'clip.mp4')
        self.assertEqual((clip['kind'], clip['size']), ('video', 100))
        self.assertIsNone(top['parent'])
        inner = list_output_dir(self.config, 'ep5')
        self.assertEqual(inner['entries'][0]['kind'], 'image')
        self.assertEqual(Path(inner['parent']), self.shared.resolve())

    def test_ranges(self):
        from server import parse_range
        self.assertIsNone(parse_range(None, 100))
        self.assertEqual(parse_range('bytes=0-9', 100), (0, 9))
        self.assertEqual(parse_range('bytes=90-', 100), (90, 99))
        self.assertEqual(parse_range('bytes=-10', 100), (90, 99))
        self.assertEqual(parse_range('bytes=50-500', 100), (50, 99))
        with self.assertRaises(ValueError):
            parse_range('bytes=100-', 100)

    def test_file_response_streams_the_requested_bytes(self):
        from server import file_response
        response = file_response(self.shared / 'clip.mp4', 'bytes=10-19')
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers['content-range'], 'bytes 10-19/100')
        self.assertEqual(response.media_type, 'video/mp4')
        async def read():
            return b''.join([chunk async for chunk in response.body_iterator])
        self.assertEqual(asyncio.run(read()), bytes(range(10, 20)))
        self.assertEqual(file_response(self.shared / 'clip.mp4', 'bytes=200-').status_code, 416)


if __name__ == '__main__':
    unittest.main()
