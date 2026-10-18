import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import sys
import threading
import socket
import time
import urllib.request
import importlib.util
import io
from types import SimpleNamespace
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]

FAKE_WORKER = '''param([string]$DataDir,[string]$Engine='codex',[string]$MainAgentName='主agent export',[switch]$FullAccess,[switch]$Restricted,[switch]$Plan,[switch]$NoDesktop)
$ErrorActionPreference='Stop'
if ($Plan) { @{engine=$Engine;data_dir=$DataDir;plan=$true} | ConvertTo-Json; return }
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$path=Join-Path $DataDir 'connection.json'
$connection=@{name='fixture-pc';main_agent_id='fixture-main';engine=$Engine;main_agent_name=$MainAgentName;token='fixture-role-token';local_url='http://127.0.0.1:18765/mcp';tailscale_url='';main_agent_workspace=(Join-Path $DataDir 'main')}
[System.IO.File]::WriteAllText($path,($connection|ConvertTo-Json),(New-Object System.Text.UTF8Encoding $false))
$client=Join-Path $DataDir 'local-client'; New-Item -ItemType Directory -Force -Path $client | Out-Null
[System.IO.File]::WriteAllText((Join-Path $client 'computers.json'),'{"computers":{}}',(New-Object System.Text.UTF8Encoding $false))
@{status='service-ready';engine=$Engine;connection_file=$path;client_home=$client;workspace=(Join-Path $DataDir 'workspace')} | ConvertTo-Json
'''


class RoleSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temp.name)
        self.home = self.root / 'orchestrator registry'
        self.data = self.root / 'worker private data'
        self.fixture = self.root / 'plugin'
        (self.fixture / 'scripts').mkdir(parents=True)
        (self.fixture / 'skills/call-agent').mkdir(parents=True)
        for name in ('setup.mjs', 'setup-orchestrator.mjs', 'setup-desktop.mjs', 'windows-outside.mjs'):
            shutil.copyfile(ROOT / 'plugin/scripts' / name, self.fixture / 'scripts' / name)
        shutil.copyfile(ROOT / 'plugin/skills/call-agent/fleet.mjs', self.fixture / 'skills/call-agent/fleet.mjs')
        (self.fixture / 'scripts/setup-worker.ps1').write_text(FAKE_WORKER, encoding='utf-8-sig')

    def tearDown(self):
        self.temp.cleanup()

    def run_setup(self, *args, actual=False):
        script = ROOT / 'plugin/scripts/setup.mjs' if actual else self.fixture / 'scripts/setup.mjs'
        env = {k: v for k, v in os.environ.items() if k not in ('CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID')}
        return subprocess.run(['node', str(script), *args, '--no-desktop'],
                              env={**env, 'AGENT_FLEET_HOME': str(self.home), 'CLAUDE_CODE_SESSION_ID': 'fixture-session', 'AGENT_FLEET_OUTSIDE': '0'},
                              capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)

    def result(self, output):
        decoder = json.JSONDecoder()
        for i, c in enumerate(output):
            if c == '{':
                try:
                    value, end = decoder.raw_decode(output[i:])
                    if not output[i+end:].strip(): return value
                except ValueError: pass
        self.fail('No final setup result')

    def test_missing_or_invalid_role_writes_nothing(self):
        for args in ((), ('--role','unknown')):
            self.assertEqual(self.run_setup(*args).returncode, 2)
            self.assertFalse(self.home.exists())
            self.assertFalse(self.data.exists())

    def test_orchestrator_only_does_not_create_worker(self):
        result = self.run_setup('--role','orchestrator')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / 'computers.json').exists())
        saved = json.loads((self.home / 'computers.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['orchestrator_session'], 'fixture-session')  # a session, no folder
        self.assertNotIn('orchestrator_workspace', saved)
        self.assertNotIn('desktop', self.result(result.stdout))
        self.assertFalse(self.data.exists())
        self.assertEqual(self.result(result.stdout)['role'], 'orchestrator')

    @unittest.skipUnless(os.name == 'nt', 'Windows worker setup')
    def test_worker_only_uses_selected_engine_without_orchestrator(self):
        result = self.run_setup('--role','worker','--data-dir',str(self.data),'--engine','claude','--restricted')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.data / 'connection.json').exists())
        self.assertFalse(self.home.exists())
        self.assertEqual(self.result(result.stdout)['worker']['engine'], 'claude')
        # The worker's own client treats the setup session as its orchestrator session.
        local = json.loads((self.data / 'local-client/computers.json').read_text(encoding='utf-8'))
        self.assertEqual(local['orchestrator_session'], 'fixture-session')

    @unittest.skipUnless(os.name == 'nt', 'Windows worker setup')
    def test_both_registers_its_worker_locally_without_exposing_token(self):
        result = self.run_setup('--role','both','--data-dir',str(self.data),'--engine','claude','--restricted')
        self.assertEqual(result.returncode, 0, result.stderr)
        registry = json.loads((self.home / 'computers.json').read_text(encoding='utf-8'))
        pc = registry['computers']['fixture-pc']
        self.assertEqual(pc['url'], 'http://127.0.0.1:18765/mcp')
        self.assertEqual(pc['engine'], 'claude')
        self.assertNotIn('fixture-role-token', result.stdout + result.stderr)
        self.assertEqual(self.result(result.stdout)['role'], 'both')

    @unittest.skipUnless(os.name == 'nt', 'Windows worker setup')
    def test_both_plan_uses_real_worker_plan_without_writes(self):
        result = self.run_setup('--role','both','--data-dir',str(self.data),'--workspace',str(self.root/'workspace'),
                                '--engine','claude','--plan',actual=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.home.exists())
        self.assertFalse(self.data.exists())
        value = self.result(result.stdout)
        self.assertTrue(value['plan'])
        self.assertTrue(value['orchestrator']['local_worker_registration'])
        self.assertEqual(value['worker']['engine'], 'claude')

    @unittest.skipUnless(os.name == 'nt', 'Windows worker setup')
    def test_dual_engine_plan_preserves_names_ids_and_workspaces(self):
        workspace = self.root / 'dual workspace'
        run = lambda *extra: self.run_setup('--role', 'worker', '--data-dir', str(self.data),
            '--workspace', str(workspace), '--plan', *extra, actual=True)
        result = run('--engines', 'codex,claude', '--main-agent-name', 'Code 经理',
                     '--second-main-agent-name', 'Claude 经理')
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = self.result(result.stdout)['worker']
        self.assertEqual(plan['engines'], ['codex', 'claude'])
        self.assertEqual([m['name'] for m in plan['main_agents']], ['Code 经理', 'Claude 经理'])
        self.assertEqual([Path(m['workspace']).name for m in plan['main_agents']], ['main-codex', 'main-claude'])
        self.assertFalse(self.data.exists())
        self.assertFalse(workspace.exists())
        self.data.mkdir()
        config = dict(engines=plan['engines'], main_agents=plan['main_agents'], executor='app-server',
                      main_agent_name='Code 经理', main_agent_id=plan['main_agents'][0]['id'], language='zh',
                      computer_name='fixture-pc', sandbox='workspace-write', port=18765, tailscale_port=18766,
                      tasks_dir=str(workspace/'AgentFleet-其他'))
        path = self.data/'config.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        before = path.read_bytes()
        repeated = run()
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(self.result(repeated.stdout)['worker']['main_agents'], plan['main_agents'])
        self.assertEqual(path.read_bytes(), before)
        for engines in ('codex,codex', 'unknown', 'claude,codex,claude', ''):
            self.assertNotEqual(run('--engines', engines).returncode, 0)
        self.assertNotEqual(run('--engines', 'codex,claude', '--second-main-agent-name', 'Code 经理').returncode, 0)
        self.assertNotEqual(run('--engines', 'codex,claude', '--engine', 'codex').returncode, 0)

    @unittest.skipUnless(os.name == 'nt', 'Windows worker setup')
    def test_direct_windows_dual_plan_is_read_only(self):
        result = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            str(ROOT/'install-worker.ps1'), '-Engines', 'claude,codex', '-Language', 'en',
            '-DataDir', str(self.data), '-Workspace', str(self.root/'workspace'), '-Plan'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = self.result(result.stdout)
        self.assertEqual(plan['engines'], ['claude', 'codex'])
        self.assertEqual([m['name'] for m in plan['main_agents']], ['Claude main agent', 'Main agent'])
        self.assertFalse(self.data.exists())

    def mac_setup(self, **overrides):
        spec = importlib.util.spec_from_file_location('fixture_mac_setup', ROOT/'worker/install_mac.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        args = dict(data_dir=str(self.data), language='en', engine=None, engines='codex,claude',
                    workspace=str(self.root/'mac workspace'), name='fixture-mac', main_agent_name=None,
                    second_main_agent_name=None, manager_name='', full_access=False, restricted=False,
                    port=None, tailscale_port=None, connectivity='local', lan_address='', json=True,
                    codex_home=str(self.root/'fixture-codex-home'), codex_version='0.162.1',
                    codex_executable=None, claude_executable=None, description='', no_start=False)
        setup = module.Setup(SimpleNamespace(**{**args, **overrides}))
        setup.resolve()
        return setup

    def test_mac_dual_config_connection_instructions_and_rerun_without_services(self):
        setup = self.mac_setup(main_agent_name='Code owner', second_main_agent_name='Claude owner')
        self.data.mkdir()
        setup.python, setup.codex, setup.claude = sys.executable, 'fixture-codex', 'fixture-claude'
        setup.claude_version = '2.1.248'
        setup.token_file, setup.token_value = self.data/'fixture-token.txt', 'fixture-only-secret'
        setup.ts_name = setup.ts_ip = None
        setup.tailscale_executable = ''
        setup.main_agent()
        setup.write_config()
        setup.connection()
        config = json.loads(setup.config_path.read_text(encoding='utf-8'))
        info = json.loads(setup.connection_file.read_text(encoding='utf-8'))
        self.assertEqual(config['main_agents'], info['main_agents'])
        self.assertEqual(config['engines'], ['codex', 'claude'])
        self.assertEqual(config['main_agent_id'], config['main_agents'][0]['id'])
        self.assertEqual(config['main_agent_workspaces'], [config['main_agents'][0]['workspace']])
        code, claude = (Path(m['workspace']) for m in config['main_agents'])
        self.assertIn('Code owner', (code/'AGENTS.md').read_text(encoding='utf-8'))
        self.assertIn('Claude owner', (claude/'CLAUDE.md').read_text(encoding='utf-8'))
        for folder, filename in ((code, 'AGENTS.md'), (claude, 'CLAUDE.md')):
            instructions = (folder/filename).read_text(encoding='utf-8')
            self.assertIn(str(folder), instructions)
            self.assertNotIn('{{', instructions)
        self.assertTrue((code/'.codex/config.toml').is_file())
        self.assertFalse((claude/'.codex/config.toml').exists())
        custom = claude/'CLAUDE.md'
        custom.write_text('owner instructions', encoding='utf-8')
        repeated = self.mac_setup(engines=None)
        self.assertEqual(repeated.mains, config['main_agents'])
        repeated.token_value = 'fixture-only-secret'
        repeated.main_agent()
        self.assertEqual(custom.read_text(encoding='utf-8'), 'owner instructions')
        for engines in ('codex,codex', 'invalid'):
            with self.assertRaises(SystemExit):
                self.mac_setup(engines=engines)

    def test_mac_checks_both_clis_and_defers_busy_restart(self):
        setup = self.mac_setup()
        fake_code, fake_claude = self.root/'codex', self.root/'claude'
        fake_code.touch(); fake_claude.touch()
        setup.args.codex_executable, setup.args.claude_executable = str(fake_code), str(fake_claude)
        calls = []
        def run(*command, **kwargs):
            calls.append(command)
            version = '0.162.1' if command[0] == str(fake_code) else '2.1.248'
            return SimpleNamespace(returncode=1 if command[-1] == 'status' else 0, stdout=version, stderr='')
        setup.run = run
        setup.agent_cli()
        self.assertIn((str(fake_code), 'login', 'status'), calls)
        self.assertIn((str(fake_claude), 'auth', 'status'), calls)
        self.assertEqual(len(setup.warnings), 2)
        setup.python, setup.token_value = Path(sys.executable), 'fixture-only-secret'
        (self.data/'state').mkdir(parents=True)
        setup.run = lambda *cmd, **kw: (calls.append(cmd) or SimpleNamespace(returncode=0))
        setup.config_path.write_text('{}', encoding='utf-8')
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"tasks":[{"status":"running","engine":"claude"}]}')):
            self.assertTrue(setup.restart())
        self.assertTrue((self.data/'state/restart-when-idle').is_file())
        self.assertFalse(any('stop' in call for call in calls))

    @unittest.skipUnless(os.name == 'nt', 'Windows worker setup')
    def test_both_reuses_existing_worker_without_changing_configuration(self):
        self.data.mkdir()
        workspace = self.root / 'existing workspace'
        token = self.data/'token.txt'; token.write_text('fixture-existing-token')
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                self.send_response(200); self.end_headers()
                self.wfile.write(b'{"service":"codex-task-mcp","status":"ok"}')
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        config = dict(executor='app-server',engine='codex',language='zh',main_agent_name='主agent export',main_agent_id='fixture-main', computer_name='fixture-pc', sandbox='workspace-write',
                      tasks_dir=str(workspace / 'AgentFleet-其他'), state_dir=str(self.data / 'state'),
                      port=server.server_port, tailscale_port=18766, remote_url='', local_only=True,token_file=str(token))
        config_path = self.data / 'config.json'
        original = json.dumps(config)
        config_path.write_text(original, encoding='utf-8')
        connection = dict(name='fixture-pc',engine='codex',main_agent_name='主agent export',main_agent_id='fixture-main',token='fixture-existing-token',
                          local_url=f'http://127.0.0.1:{server.server_port}/mcp',tailscale_url='',main_agent_workspace=str(workspace/'main'))
        (self.data / 'connection.json').write_text(json.dumps(connection), encoding='utf-8')
        try:
            result = self.run_setup('--role','both','--data-dir',str(self.data),'--restricted', actual=True)
        finally:
            server.shutdown(); server.server_close(); thread.join()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config_path.read_text(encoding='utf-8'), original)
        self.assertFalse(workspace.exists())
        final = self.result(result.stdout)
        self.assertEqual(final['worker']['status'], 'existing-worker')
        registry = json.loads((self.home / 'computers.json').read_text(encoding='utf-8'))
        self.assertIn('fixture-pc', registry['computers'])
        self.assertNotIn('fixture-existing-token', result.stdout + result.stderr)

    @unittest.skipUnless(os.name == 'nt', 'Windows worker setup')
    def test_rerun_after_plugin_update_plans_a_redeploy(self):
        self.data.mkdir()
        workspace = self.root/'existing workspace'
        service = workspace/'service/worker'; service.mkdir(parents=True)
        (service/'server.py').write_text('# old code', encoding='utf-8')
        token = self.data/'token.txt'; token.write_text('fixture-plan-token')
        config = dict(executor='app-server',engine='codex',language='zh',main_agent_name='主agent export',main_agent_id='fixture-main',computer_name='fixture-pc',sandbox='workspace-write',
            tasks_dir=str(workspace/'AgentFleet-其他'),state_dir=str(self.data/'state'),token_file=str(token),
            port=18765,tailscale_port=18766,remote_url='',local_only=True)
        (self.data/'config.json').write_text(json.dumps(config), encoding='utf-8')
        plan = lambda: self.result(self.run_setup('--role','worker','--data-dir',str(self.data),'--restricted','--plan',actual=True).stdout)['worker']
        self.assertTrue(plan()['update'], 'old code without a version stamp is redeployed')
        (workspace/'service/plugin-version.txt').write_text('0.0.1', encoding='utf-8')
        self.assertTrue(plan()['update'], 'code from an older plugin version is redeployed')
        version = json.loads((ROOT/'plugin/plugin.json').read_text(encoding='utf-8'))['version']
        (workspace/'service/plugin-version.txt').write_text(version, encoding='utf-8')
        self.assertFalse(plan()['update'], 'current code is reused')

    @unittest.skipUnless(os.name == 'nt', 'Windows worker setup')
    def test_reused_stopped_worker_is_started_without_rewriting_config(self):
        self.data.mkdir()
        workspace = self.root/'existing workspace'
        service = workspace/'service/worker'; service.mkdir(parents=True)
        for filename in ('start.ps1','start_service.py'):
            shutil.copyfile(ROOT/'worker'/filename,service/filename)
        version = json.loads((ROOT/'plugin/plugin.json').read_text(encoding='utf-8'))['version']
        (workspace/'service/plugin-version.txt').write_text(version, encoding='utf-8')
        (service/'server.py').write_text('''import argparse,json,os
from http.server import BaseHTTPRequestHandler,HTTPServer
p=argparse.ArgumentParser();p.add_argument('--config');a=p.parse_args()
c=json.load(open(a.config))
class H(BaseHTTPRequestHandler):
 def do_GET(self):
  b=json.dumps({'service':'codex-task-mcp','status':'ok','pid':os.getpid()}).encode()
  self.send_response(200);self.end_headers();self.wfile.write(b)
HTTPServer(('127.0.0.1',c['port']),H).serve_forever()
''',encoding='utf-8')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        token=self.data/'token.txt'; token.write_text('fixture-start-token')
        config=dict(executor='app-server',engine='codex',language='zh',main_agent_name='主agent export',main_agent_id='fixture-main',computer_name='fixture-pc',sandbox='workspace-write',
            tasks_dir=str(workspace/'AgentFleet-其他'),state_dir=str(self.data/'state'),token_file=str(token),
            python_executable=sys.executable,port=port,tailscale_port=18766,remote_url='',local_only=True)
        path=self.data/'config.json'; original=json.dumps(config); path.write_text(original,encoding='utf-8')
        try:
            result=self.run_setup('--role','worker','--data-dir',str(self.data),'--restricted',actual=True)
            self.assertEqual(result.returncode,0,result.stderr)
            final=self.result(result.stdout)
            self.assertEqual(final['worker']['service_status'],'ok')
            self.assertEqual(path.read_text(encoding='utf-8'),original)
            self.assertNotIn('fixture-start-token',result.stdout+result.stderr)
        finally:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=2) as response: health=json.load(response)
                subprocess.run(['taskkill.exe','/PID',str(health['pid']),'/T','/F'],stdout=subprocess.DEVNULL,check=True)
                time.sleep(.2)
            except OSError: pass

    @unittest.skipUnless(os.name == 'nt', 'Windows worker setup')
    def test_worker_failure_does_not_initialize_orchestrator(self):
        (self.fixture / 'scripts/setup-worker.ps1').write_text('exit 7',encoding='utf-8-sig')
        result = self.run_setup('--role','both','--data-dir',str(self.data),'--restricted')
        self.assertEqual(result.returncode, 7)
        self.assertFalse(self.home.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows worker setup')
    def test_both_passes_main_name_and_registers_it(self):
        chosen = 'main agent windows pc 1'
        result = self.run_setup('--role','both','--data-dir',str(self.data),'--main-agent-name',chosen,'--restricted')
        self.assertEqual(result.returncode, 0, result.stderr)
        pc = json.loads((self.home/'computers.json').read_text(encoding='utf-8'))['computers']['fixture-pc']
        self.assertEqual(pc['main_agent_name'], chosen)
        self.assertIn(chosen, pc['agents'])
        self.assertNotIn('主agent export', pc['agents'])

    @unittest.skipUnless(os.name == 'nt', 'Windows worker setup')
    def test_custom_worker_name_plan_does_not_write(self):
        result = self.run_setup('--role','worker','--data-dir',str(self.data),'--main-agent-name','Main Agent 工作 1','--plan',actual=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.result(result.stdout)['worker']['main_agent_name'],'Main Agent 工作 1')
        self.assertFalse(self.data.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows worker setup')
    def test_worker_invalid_name_is_rejected_before_writes(self):
        for name in ('', 'x'*101, '--new'):
            result = self.run_setup('--role','worker','--data-dir',str(self.data),'--main-agent-name',name,'--plan',actual=True)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse(self.data.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows worker setup')
    def test_unicode_registry_collision_is_rejected_before_plan(self):
        chosen = 'Main Agent 工作 1'
        (self.data/'local-client').mkdir(parents=True)
        registry = {'computers':{'fixture-pc':{'agents':{chosen:{'thread_id':'keep'}}}}}
        (self.data/'local-client/computers.json').write_text(json.dumps(registry,ensure_ascii=False),encoding='utf-8')
        result = self.run_setup('--role','worker','--name','fixture-pc','--data-dir',str(self.data),'--main-agent-name',chosen,'--plan',actual=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('already uses',result.stderr)

    @unittest.skipUnless(os.name == 'nt', 'Windows worker setup')
    def test_unicode_profile_collision_is_rejected_before_deployment(self):
        chosen = 'Main Agent 工作 1'
        (self.data/'state').mkdir(parents=True)
        token = self.data/'token.txt'; token.write_text('fixture-name-token')
        workspace = self.root/'workspace'
        config = dict(executor='app-server',engine='codex',language='zh',main_agent_name='主agent export',main_agent_id='fixture-main',computer_name='fixture-pc',sandbox='workspace-write',
            tasks_dir=str(workspace/'AgentFleet-其他'),state_dir=str(self.data/'state'),token_file=str(token),
            port=1,tailscale_port=18766,remote_url='',local_only=True)
        before = json.dumps(config)
        (self.data/'config.json').write_text(before,encoding='utf-8')
        (self.data/'state/profile.json').write_text(json.dumps({'agents':{chosen:{'type':'resident'}}},ensure_ascii=False),encoding='utf-8')
        result = self.run_setup('--role','worker','--data-dir',str(self.data),'--main-agent-name',chosen,'--restricted','--reconfigure',actual=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('already uses',result.stderr)
        self.assertFalse(workspace.exists())
        self.assertEqual((self.data/'config.json').read_text(encoding='utf-8'),before)

    def test_worker_setup_requires_a_permission_choice(self):
        for args in (('--role','worker','--data-dir',str(self.data)), ('--role','both','--data-dir',str(self.data)),
                     ('--role','worker','--data-dir',str(self.data),'--full-access','--restricted')):
            result = self.run_setup(*args)
            self.assertEqual(result.returncode, 2)
            self.assertIn('--restricted', result.stderr)
            self.assertFalse(self.home.exists())
            self.assertFalse(self.data.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows worker setup')
    def test_changing_permissions_needs_reconfigure(self):
        self.data.mkdir()
        workspace = self.root/'workspace'
        config = dict(executor='app-server',engine='codex',language='zh',main_agent_name='主agent export',main_agent_id='fixture-main',computer_name='fixture-pc',sandbox='danger-full-access',auto_approve=True,
            tasks_dir=str(workspace/'AgentFleet-其他'),state_dir=str(self.data/'state'),token_file=str(self.data/'token.txt'),
            port=1,tailscale_port=18766,remote_url='',local_only=True)
        before = json.dumps(config)
        (self.data/'config.json').write_text(before,encoding='utf-8')
        result = self.run_setup('--role','worker','--data-dir',str(self.data),'--name','fixture-pc','--restricted',actual=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('-Reconfigure',result.stderr)
        self.assertEqual((self.data/'config.json').read_text(encoding='utf-8'),before)
        self.assertFalse(workspace.exists())

    def test_wrong_role_options_are_rejected_before_writes(self):
        for args in (('--role','orchestrator','--engine','claude'), ('--role','worker','--registry-home',str(self.home)),
                     ('--role','worker','--connectivity','lan','--lan-address'), ('--role','worker','--lan-address','192.0.2.10'),
                     ('--role','worker','--connectivity','wifi'), ('--role','orchestrator','--connectivity','lan'), ('--role','orchestrator','--restricted')):
            self.assertEqual(self.run_setup(*args).returncode, 2)
            self.assertFalse(self.home.exists())


if __name__ == '__main__':
    unittest.main()
