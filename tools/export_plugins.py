"""Export the reviewed source and plugin directories without machine data."""
import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
FILES = ('README.md', 'PLUGINS.md', 'AGENTS.md', 'CLAUDE.md', 'LICENSE', '.gitignore',
         'Install-Plugins.ps1', 'install-worker.ps1', 'install-orchestrator.ps1')
DIRECTORIES = ('.agents', '.claude-plugin', 'plugin', 'worker', 'tools', 'tests')


def export(destination):
    destination = destination.resolve()
    if destination == ROOT or destination.is_relative_to(ROOT):
        raise ValueError('Export destination must be outside the source repository')
    destination.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        shutil.copyfile(ROOT / name, destination / name)
    for name in DIRECTORIES:
        shutil.copytree(ROOT / name, destination / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
    forbidden = ('token.txt', 'connection.json', 'auth.json', 'config.toml', 'config.json', 'events.jsonl')
    bad = [p for p in destination.rglob('*') if p.is_file() and (p.name in forbidden or p.suffix == '.log')]
    if bad:
        raise ValueError('Export contains runtime/private files; do not distribute it')
    print('Exported self-contained plugins, installation helper, guide and source. No runtime or credential files included.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', type=Path)
    export(parser.parse_args().destination)
