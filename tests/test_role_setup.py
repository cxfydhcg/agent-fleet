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
        self.temp = tempfile.TemporaryDirectory(dir=ROOT.parent)
        self.root = Path(self.temp.name)
        self.home = self.root / 'orchestrator registry'
        self.data = self.root / 'worker private data'
        self.fixture = self.root / 'plugin'
        (self.fixture / 'scripts').mkdir(parents=True)
        (self.fixture / 'skills/call-agent').mkdir(parents=True)
        for name in ('setup.mjs', 'setup-orchestrator.mjs', 'setup-desktop.mjs'):
            shutil.copyfile(ROOT / 'plugin/scripts' / name, self.fixture / 'scripts' / name)
        shutil.copyfile(ROOT / 'plugin/skills/call-agent/fleet.mjs', self.fixture / 'skills/call-agent/fleet.mjs')
        (self.fixture / 'scripts/setup-worker.ps1').write_text(FAKE_WORKER, encoding='utf-8-sig')

    def tearDown(self):
        self.temp.cleanup()

    def run_setup(self, *args, actual=False):
        script = ROOT / 'plugin/scripts/setup.mjs' if actual else self.fixture / 'scripts/setup.mjs'
        return subprocess.run(['node', str(script), *args, '--no-desktop'], env={**os.environ, 'AGENT_FLEET_HOME': str(self.home)},
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
        self.assertEqual(Path(saved['orchestrator_workspace']), Path.home() / 'Documents' / 'AgentFleet')
        self.assertFalse(self.data.exists())
        self.assertEqual(self.result(result.stdout)['role'], 'orchestrator')

    @unittest.skipUnless(os.name == 'nt', 'Windows worker setup')
    def test_worker_only_uses_selected_engine_without_orchestrator(self):
        result = self.run_setup('--role','worker','--data-dir',str(self.data),'--engine','claude','--restricted')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.data / 'connection.json').exists())
        self.assertFalse(self.home.exists())
        self.assertEqual(self.result(result.stdout)['worker']['engine'], 'claude')
        # The worker's own client treats its workspace as the orchestrator folder.
        local = json.loads((self.data / 'local-client/computers.json').read_text(encoding='utf-8'))
        self.assertEqual(Path(local['orchestrator_workspace']), (self.data / 'workspace').resolve())

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
    def test_reused_stopped_worker_is_started_without_rewriting_config(self):
        self.data.mkdir()
        workspace = self.root/'existing workspace'
        service = workspace/'service/worker'; service.mkdir(parents=True)
        for filename in ('start.ps1','start_service.py'):
            shutil.copyfile(ROOT/'worker'/filename,service/filename)
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
