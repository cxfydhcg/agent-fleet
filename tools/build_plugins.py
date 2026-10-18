"""Build one self-contained Agent Fleet plugin with role-selecting setup."""
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def build():
    root = ROOT / 'plugin'
    metadata = dict(name='agent-fleet', version='0.6.20',
                    description='Set up this computer as an orchestrator, worker, or both in Codex or Claude Code.')
    extension = dict(onboardingSkill='./skills/setup/SKILL.md', interface=dict(
        displayName='Agent Fleet', shortDescription=metadata['description'],
        developerName='Agent Fleet', category='Productivity'))
    write_json(root / 'plugin.json', {'$schema': 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json',
               **metadata, 'extensions': {'com.openai': extension}})
    write_json(root / '.codex-plugin/plugin.json', {**metadata, 'skills': './skills/',
               'interface': extension['interface'], 'extensions': {'com.openai': {'onboardingSkill': extension['onboardingSkill']}}})
    write_json(root / '.claude-plugin/plugin.json', {**metadata, 'author': {'name': 'Agent Fleet'},
               'license': 'MIT', 'skills': './skills/'})
    shutil.copyfile(ROOT / 'LICENSE', root / 'LICENSE')
    shutil.copyfile(root / 'skills/call-agent/fleet.mjs', root / 'scripts/fleet.mjs')
    runtime = root / 'runtime'
    runtime.mkdir(exist_ok=True)
    shutil.copyfile(ROOT / 'install-worker.ps1', runtime / 'install-worker.ps1')
    shutil.copy2(ROOT / 'install-worker.sh', runtime / 'install-worker.sh')  # keeps the executable bit
    shutil.copyfile(ROOT / 'LICENSE', runtime / 'LICENSE')
    for path in (ROOT / 'worker').rglob('*'):
        if not path.is_file() or '__pycache__' in path.parts or path.suffix == '.pyc':
            continue
        target = runtime / 'worker' / path.relative_to(ROOT / 'worker')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    for path in root.rglob('*.ps1'):
        path.write_text(path.read_text(encoding='utf-8-sig'), encoding='utf-8-sig')

    write_json(ROOT / '.agents/plugins/marketplace.json', {'name': 'agent-fleet',
        'interface': {'displayName': 'Agent Fleet'}, 'plugins': [{'name': 'agent-fleet',
        'source': {'source': 'local', 'path': './plugin'}, 'category': 'Productivity',
        'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'}}]})
    write_json(ROOT / '.claude-plugin/marketplace.json', {'name': 'agent-fleet',
        'description': 'Set up and manage an agent fleet from Codex or Claude Code.',
        'owner': {'name': 'Xufeng Ce'}, 'plugins': [{'name': 'agent-fleet', 'source': './plugin',
        'description': metadata['description']}]})
    print('Built one Agent Fleet plugin with orchestrator, worker and both setup roles.')


if __name__ == '__main__':
    build()
