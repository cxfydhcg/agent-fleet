"""Put the dispatcher's sessions into one sidebar folder of the Codex desktop app.

The desktop app (26.1007) does not show app-server projects yet. Its sidebar folders come from its own
internal file ~/.codex/.codex-global-state.json, which this edits: it may change with any app update.
Quit the desktop app first; it rewrites the file while running.

    python codex_app_folder.py --config <dispatcher config.json> [--folder-name AgentFleet] [--root <path>] [--dry-run]

Only the standard library is used.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

STATE_FILE = '.codex-global-state.json'
# The Windows desktop app runs as ChatGPT.exe. codex.exe is the CLI, which the dispatcher also runs, so it
# is not a sign that the app is open.
DESKTOP_PROCESSES = {'nt': ['chatgpt.exe'], 'posix': ['ChatGPT', 'Codex']}


def same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def add_to_folder(state, folder_name, root, thread_ids, now_ms, new_id):
    """Return (new state, report): a local project for root (created if no project has that root), with
    every thread in thread_ids that has no folder yet assigned to it. Threads the user already put in a
    folder are left alone. Pure: the input is not modified."""
    state = json.loads(json.dumps(state))
    projects = state.setdefault('local-projects', {})
    order = state.setdefault('project-order', [])
    assignments = state.setdefault('thread-project-assignments', {})
    projectless = state.setdefault('projectless-thread-ids', [])
    project = next((p for p in projects.values() if any(same_path(r, root) for r in p.get('rootPaths') or [])), None)
    created = project is None
    if created:
        project = {'id': new_id, 'name': folder_name, 'rootPaths': [root], 'createdAt': now_ms, 'updatedAt': now_ms}
        projects[new_id] = project
        order.insert(0, new_id)
    assigned = [t for t in dict.fromkeys(thread_ids) if t and t not in assignments]
    for thread in assigned:
        assignments[thread] = {'projectKind': 'local', 'projectId': project['id']}
    state['projectless-thread-ids'] = [t for t in projectless if t not in set(assigned)]
    return state, {'project_id': project['id'], 'project_name': project['name'], 'created': created,
                   'assigned': assigned, 'already_assigned': len(set(thread_ids) - set(assigned))}


def dispatcher_threads(state_dir):
    """Every session the dispatcher created, including forks and the originals they were forked from."""
    threads = []
    for file in sorted(Path(state_dir, 'jobs').glob('*/job.json')):
        job = json.loads(file.read_text(encoding='utf-8'))
        threads += [job[k] for k in ('thread_id', 'forked_from_thread_id') if job.get(k)]
    return list(dict.fromkeys(threads))


def desktop_app_running():
    if os.name == 'nt':
        listed = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True, text=True,
                                errors='replace', check=True).stdout
        names = {line.split('","')[0].strip('"').lower() for line in listed.splitlines() if line.strip()}
        return [n for n in DESKTOP_PROCESSES['nt'] if n in names]
    return [n for n in DESKTOP_PROCESSES['posix'] if shutil.which('pgrep')
            and subprocess.run(['pgrep', '-x', n], capture_output=True).returncode == 0]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--config', required=True, help="the dispatcher's config.json")
    parser.add_argument('--folder-name', default='AgentFleet')
    parser.add_argument('--root', help="the folder's root path (default: the parent of the config's tasks_dir)")
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    sys.stdout.reconfigure(errors='replace')  # Chinese names on a non-UTF-8 console must not abort mid-run
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    root = args.root or (str(Path(config['tasks_dir']).resolve().parent) if config.get('tasks_dir') else None)
    if not root:
        parser.error('--root is required when the config has no tasks_dir')
    codex_home = Path(config.get('codex_home') or os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    path = codex_home / STATE_FILE
    if not path.exists():
        sys.exit(f'{path} not found; open the Codex desktop app once, then quit it and retry.')
    running = desktop_app_running()
    if running:
        sys.exit(f'The Codex desktop app is running ({", ".join(running)}). Quit it completely first; it overwrites {path.name}.')

    state = json.loads(path.read_text(encoding='utf-8'))
    threads = dispatcher_threads(config['state_dir'])
    updated, report = add_to_folder(state, args.folder_name, os.path.normpath(root), threads,
                                    int(time.time() * 1000), str(uuid.uuid4()))
    action = 'Would create' if args.dry_run else 'Created'
    print(f"{action if report['created'] else 'Using existing'} folder \"{report['project_name']}\" "
          f"({report['project_id']}) for {root}")
    print(f"{'Would assign' if args.dry_run else 'Assigned'} {len(report['assigned'])} session(s); "
          f"{report['already_assigned']} already had a folder.")
    if args.dry_run or (not report['created'] and not report['assigned']):
        return
    backup = path.with_name(f"{path.name}.bak-agent-fleet-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(updated, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    temporary.replace(path)
    check = json.loads(path.read_text(encoding='utf-8'))
    if any(check['thread-project-assignments'].get(t, {}).get('projectId') != report['project_id'] for t in report['assigned']):
        shutil.copy2(backup, path)
        sys.exit(f'Validation failed; restored {path.name} from {backup.name}.')
    print(f'Wrote {path} (backup: {backup.name}). Start the Codex desktop app to see the folder.')


if __name__ == '__main__':
    main()
