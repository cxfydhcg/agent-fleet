import json
from pathlib import Path
import subprocess
import shutil
import socket
import sys
import urllib.request
import tempfile
import unittest
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / 'plugin/scripts/setup-orchestrator.mjs'
WORKER = ROOT / 'plugin/scripts/setup-worker.ps1'


class PluginSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT.parent)
        self.root = Path(self.temp.name)
        self.home = self.root / 'registry'

    def tearDown(self):
        # Windows can briefly retain a terminated fixture process's log handle.
        for attempt in range(20):
            try:
                self.temp.cleanup()
                return
            except PermissionError:
                if attempt == 19: raise
                time.sleep(0.1)

    def orchestrator(self, *args):
        return subprocess.run(['node', str(ORCH), '--home', str(self.home), '--no-desktop', *args],
                              capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)

    def connection(self, **overrides):
        path = self.root / 'connection.json'
        info = dict(name='fixture-pc', token='fixture-only-token', local_url='http://127.0.0.1:8765/mcp',
                    tailscale_url='', main_agent_workspace=str(self.root / 'main'))
        info.update(overrides)
        path.write_text(json.dumps(info), encoding='utf-8')
        return path

    def test_repeated_setup_preserves_existing_registry_and_token(self):
        self.assertEqual(self.orchestrator().returncode, 0)
        registry = self.home / 'computers.json'
        contents = {'computers': {'existing-pc': {'description': 'keep me'}}, 'custom': 42}
        registry.write_text(json.dumps(contents), encoding='utf-8')
        token = self.home / 'tokens/existing-pc.txt'
        token.write_text('fixture-old-token', encoding='utf-8')
        self.assertEqual(self.orchestrator().returncode, 0)
        # Setup also records the orchestrator's folder; everything else is kept as it was.
        self.assertEqual(json.loads(registry.read_text(encoding='utf-8')), {**contents, 'orchestrator_workspace': str(Path.home() / 'Documents' / 'AgentFleet')})
        self.assertEqual(token.read_text(), 'fixture-old-token')

    def test_local_registration_uses_local_url_and_redacts_token(self):
        result = self.orchestrator('--connection', str(self.connection()), '--local')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('fixture-only-token', result.stdout + result.stderr)
        value = json.loads((self.home / 'computers.json').read_text())['computers']['fixture-pc']
        self.assertEqual(value['url'], 'http://127.0.0.1:8765/mcp')
        self.assertEqual(Path(value['token_file']).read_text().strip(), 'fixture-only-token')

    def test_remote_registration_requires_remote_address_before_mutation(self):
        result = self.orchestrator('--connection', str(self.connection()))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.home.exists())

    def test_remote_registration_uses_remote_address(self):
        url = 'http://fixture.tail.example:8766/mcp'
        result = self.orchestrator('--connection', str(self.connection(tailscale_url=url)))
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads((self.home / 'computers.json').read_text())['computers']['fixture-pc']
        self.assertEqual(value['url'], url)

    def test_lan_registration_uses_lan_address(self):
        url = 'http://192.0.2.10:8765/mcp'
        result = self.orchestrator('--connection', str(self.connection(lan_url=url)))
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads((self.home / 'computers.json').read_text())['computers']['fixture-pc']
        self.assertEqual(value['url'], url)
        self.assertEqual(value['lan_url'], url)
        self.assertNotIn('fixture-only-token', result.stdout + result.stderr)

    def test_engine_switch_updates_workspace_and_drops_incompatible_session_pointer(self):
        self.assertEqual(self.orchestrator('--connection', str(self.connection(engine='codex')), '--local').returncode, 0)
        registry = self.home / 'computers.json'
        value = json.loads(registry.read_text(encoding='utf-8'))
        value['computers']['fixture-pc']['agents']['主agent export'].update(last_task_id='old-codex-task', thread_id='old-codex-session')
        registry.write_text(json.dumps(value), encoding='utf-8')
        new_workspace = str(self.root / 'new main')
        self.assertEqual(self.orchestrator('--connection', str(self.connection(engine='claude',main_agent_workspace=new_workspace)), '--local').returncode, 0)
        pc = json.loads(registry.read_text(encoding='utf-8'))['computers']['fixture-pc']
        self.assertEqual(pc['engine'], 'claude')
        self.assertEqual(pc['agents']['主agent export']['workspace'], new_workspace)
        self.assertNotIn('last_task_id', pc['agents']['主agent export'])

    def test_path_traversal_name_is_rejected_before_mutation(self):
        result = self.orchestrator('--connection', str(self.connection(name='../escape')), '--local')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.home.exists())

    def test_main_agent_rename_preserves_session_and_other_agents(self):
        self.assertEqual(self.orchestrator('--connection', str(self.connection()), '--local').returncode, 0)
        registry = self.home / 'computers.json'
        value = json.loads(registry.read_text(encoding='utf-8'))
        agents = value['computers']['fixture-pc']['agents']
        agents['主agent export'].update(last_task_id='existing-task', thread_id='existing-session')
        agents['another agent'] = {'thread_id': 'other-session'}
        registry.write_text(json.dumps(value))
        chosen = 'main agent windows pc 1'
        result = self.orchestrator('--connection', str(self.connection(main_agent_name=chosen)), '--local')
        self.assertEqual(result.returncode, 0, result.stderr)
        pc = json.loads(registry.read_text(encoding='utf-8'))['computers']['fixture-pc']
        self.assertEqual(pc['main_agent_name'], chosen)
        self.assertEqual(pc['agents'][chosen]['thread_id'], 'existing-session')
        self.assertEqual(pc['agents'][chosen]['last_task_id'], 'existing-task')
        self.assertEqual(pc['agents']['another agent']['thread_id'], 'other-session')
        self.assertNotIn('主agent export', pc['agents'])
        # Older connection files must not reset an already-customized name.
        self.assertEqual(self.orchestrator('--connection', str(self.connection()), '--local').returncode, 0)
        self.assertEqual(json.loads(registry.read_text(encoding='utf-8'))['computers']['fixture-pc']['main_agent_name'], chosen)

    def test_invalid_main_name_is_rejected_before_setup_writes(self):
        for name in ('', 'x' * 101, 'bad\nname', '__proto__', '--new'):
            result = self.orchestrator('--connection', str(self.connection(main_agent_name=name)), '--local')
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse(self.home.exists())

    def test_main_rename_collision_preserves_registry_and_token(self):
        self.assertEqual(self.orchestrator('--connection', str(self.connection()), '--local').returncode, 0)
        registry = self.home / 'computers.json'
        value = json.loads(registry.read_text(encoding='utf-8'))
        value['computers']['fixture-pc']['agents']['Already Used'] = {'thread_id': 'do-not-overwrite'}
        registry.write_text(json.dumps(value))
        before = registry.read_bytes()
        token = self.home / 'tokens/fixture-pc.txt'
        before_token = token.read_bytes()
        result = self.orchestrator('--connection', str(self.connection(main_agent_name='Already Used', token='replacement-token')), '--local')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(registry.read_bytes(), before)
        self.assertEqual(token.read_bytes(), before_token)

    def test_export_routes_to_custom_main_and_resumes_session(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if request['method'] == 'initialize':
                    result = {}
                else:
                    params = request['params']; calls.append(params)
                    task = {'task_id': 'renamed-turn', 'thread_id': 'existing-session', 'status': 'completed', 'result': 'OK'}
                    result = {'content': [{'type':'text','text':json.dumps(task)}]}
                body = json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)
        with ThreadingHTTPServer(('127.0.0.1',0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                url = f'http://127.0.0.1:{server.server_port}/mcp'
                chosen = 'Main Agent 工作电脑 1'
                self.assertEqual(self.orchestrator('--connection', str(self.connection(main_agent_name=chosen,local_url=url)), '--local').returncode, 0)
                registry = self.home / 'computers.json'
                value = json.loads(registry.read_text(encoding='utf-8'))
                value['computers']['fixture-pc']['agents'][chosen]['last_task_id'] = 'existing-task'
                value['orchestrator_workspace'] = str(self.root)  # export runs as the orchestrator session
                registry.write_text(json.dumps(value))
                result = subprocess.run(['node',str(ROOT/'plugin/scripts/fleet.mjs'),'export','fixture-pc','Reply OK'],
                    env={**os.environ,'AGENT_FLEET_HOME':str(self.home)},cwd=self.root,capture_output=True,text=True,encoding='utf-8',timeout=20)
                self.assertEqual(result.returncode,0,result.stderr)
                answer = json.loads(result.stdout)
                self.assertEqual(answer['agent'], chosen)
                self.assertEqual(answer['thread_id'], 'existing-session')
                # export first asks for the main agent by id; a worker that has none falls back to the saved session.
                calls = [c for c in calls if c['name'] != 'get_profile']
                self.assertEqual(calls[0]['name'], 'continue_task')
                self.assertEqual(calls[0]['arguments']['task_id'], 'existing-task')
                self.assertNotIn('submit_task',[c['name'] for c in calls])
            finally:
                server.shutdown(); thread.join()

    def test_missing_argument_is_rejected_before_mutation(self):
        self.assertEqual(self.orchestrator('--connection').returncode, 2)
        self.assertFalse(self.home.exists())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows worker setup')
    def test_worker_plan_does_not_install_or_write_data(self):
        data, workspace = self.root / 'private', self.root / 'workspace with spaces'
        result = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(WORKER),
            '-Plan', '-Engine', 'claude', '-DataDir', str(data), '-Workspace', str(workspace)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['engine'], 'claude')
        self.assertEqual(value['connectivity'], 'Local')
        self.assertFalse(value['full_access'])
        self.assertFalse(data.exists())
        self.assertFalse(workspace.exists())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows worker setup')
    def test_lan_worker_plan_reports_address_without_writes(self):
        data, workspace = self.root / 'private', self.root / 'workspace'
        run = lambda *extra: subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(WORKER),
            '-Plan', '-DataDir', str(data), '-Workspace', str(workspace), *extra],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
        result = run('-Connectivity', 'Lan', '-LanAddress', '192.0.2.10')
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['connectivity'], 'Lan')
        self.assertEqual(value['lan_address'], '192.0.2.10')
        self.assertNotEqual(run('-Connectivity', 'Local', '-LanAddress', '192.0.2.10').returncode, 0)
        self.assertFalse(data.exists())
        self.assertFalse(workspace.exists())

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS worker setup')
    def test_mac_worker_plan_does_not_install_or_write_data(self):
        data, workspace = self.root / 'private', self.root / 'workspace with spaces'
        run = lambda *extra: subprocess.run(['node', str(ROOT / 'plugin/scripts/setup.mjs'), '--role', 'worker', '--plan', '--no-desktop',
            '--data-dir', str(data), '--workspace', str(workspace), *extra], capture_output=True, text=True, encoding='utf-8', timeout=60)
        result = run('--engine', 'claude')
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout[result.stdout.rindex('{\n  "role"'):])['worker']
        self.assertEqual((value['platform'], value['engine'], value['connectivity'], value['full_access']), ('macos', 'claude', 'local', False))
        result = run('--connectivity', 'lan', '--lan-address', '192.0.2.10', '--language', 'en')
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout[result.stdout.rindex('{\n  "role"'):])['worker']
        self.assertEqual((value['connectivity'], value['lan_address'], value['main_agent_name']), ('lan', '192.0.2.10', 'Main agent'))
        self.assertEqual(Path(value['tasks_dir']), workspace / 'AgentFleet-Other')
        result = run('--language', 'zh')
        value = json.loads(result.stdout[result.stdout.rindex('{\n  "role"'):])['worker']
        self.assertEqual(Path(value['tasks_dir']), workspace / 'AgentFleet-其他')
        self.assertNotEqual(run('--connectivity', 'local', '--lan-address', '192.0.2.10').returncode, 0)
        self.assertFalse(data.exists())
        self.assertFalse(workspace.exists())

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS worker setup')
    def test_mac_reinstall_preserves_named_task_directory(self):
        data, workspace = self.root / 'private', self.root / 'workspace'
        data.mkdir()
        path = data / 'config.json'
        original = json.dumps({'language': 'en', 'tasks_dir': str(workspace / 'AgentFleet-Other'),
                               'sandbox': 'danger-full-access', 'computer_name': 'fixture-pc'})
        path.write_text(original)
        result = subprocess.run(['node', str(ROOT / 'plugin/scripts/setup.mjs'), '--role', 'worker', '--plan',
            '--no-desktop', '--data-dir', str(data)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout[result.stdout.rindex('{\n  "role"'):])['worker']
        self.assertEqual(Path(value['tasks_dir']), workspace / 'AgentFleet-Other')
        self.assertEqual(path.read_text(), original)
        self.assertFalse(workspace.exists())

    def test_manifests_and_catalogs_reference_self_contained_plugins(self):
        catalog = json.loads((ROOT / '.agents/plugins/marketplace.json').read_text())
        self.assertEqual({p['name'] for p in catalog['plugins']}, {'agent-fleet'})
        for entry in catalog['plugins']:
            plugin = (ROOT / entry['source']['path']).resolve()
            self.assertTrue(plugin.is_relative_to(ROOT))
            portable = json.loads((plugin / 'plugin.json').read_text())
            for manifest in ('.claude-plugin/plugin.json', '.codex-plugin/plugin.json'):
                value = json.loads((plugin / manifest).read_text())
                self.assertEqual((value['name'], value['version']), (portable['name'], portable['version']))
            onboarding = portable['extensions']['com.openai']['onboardingSkill']
            self.assertTrue((plugin / onboarding).is_file())
            self.assertEqual(onboarding, './skills/setup/SKILL.md')
            self.assertTrue((plugin / 'runtime/worker/claude_executor.py').is_file())
            self.assertTrue((plugin / 'runtime/install-worker.ps1').is_file())
            self.assertTrue((plugin / 'runtime/install-worker.sh').is_file())
            self.assertTrue((plugin / 'runtime/worker/install_mac.py').is_file())
            for skill in ('setup', 'start', 'pair', 'call-agent', 'main-agent'):
                self.assertTrue((plugin / f'skills/{skill}/SKILL.md').is_file(), skill)
            self.assertTrue((plugin / 'runtime/worker/tools/pairing.py').is_file())  # what the pair skill runs on a worker
            self.assertTrue((plugin / 'scripts/setup.mjs').is_file())
            self.assertTrue((plugin / 'scripts/start.mjs').is_file())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows worker startup')
    def test_background_worker_does_not_hold_installer_capture_pipes(self):
        service, data = self.root / 'service with spaces', self.root / 'data'
        service.mkdir(); data.mkdir()
        for name in ('start.ps1', 'start_service.py'):
            shutil.copyfile(ROOT / 'worker' / name, service / name)
        (service / 'server.py').write_text('''import argparse,json,os
from http.server import BaseHTTPRequestHandler,HTTPServer
p=argparse.ArgumentParser();p.add_argument('--config');a=p.parse_args()
c=json.load(open(a.config))
class H(BaseHTTPRequestHandler):
 def do_GET(self):
  b=json.dumps({'service':'codex-task-mcp','pid':os.getpid()}).encode()
  self.send_response(200);self.end_headers();self.wfile.write(b)
HTTPServer(('127.0.0.1',c['port']),H).serve_forever()
''', encoding='utf-8')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        token = data / 'token.txt'; token.write_text('fixture-launch-token')
        config = data / 'config.json'
        config.write_text(json.dumps(dict(port=port, python_executable=sys.executable,
            state_dir=str(data / 'state'), token_file=str(token), local_only=True)))
        try:
            result = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                str(service / 'start.ps1'), '-ConfigPath', str(config), '-LocalOnly'],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Local MCP:', result.stdout)
        finally:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=2) as response:
                    health = json.load(response)
                subprocess.run(['taskkill.exe', '/PID', str(health['pid']), '/T', '/F'],
                               stdout=subprocess.DEVNULL, check=True)
            except OSError:
                pass


if __name__ == '__main__':
    unittest.main()
