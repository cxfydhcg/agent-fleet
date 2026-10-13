"""Start the service without inheriting an agent terminal's capture pipes."""
import argparse
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    state = Path(config['state_dir'])
    state.mkdir(parents=True, exist_ok=True)
    command = [config['python_executable'], '-u', str(Path(__file__).with_name('server.py')),
               '--config', str(args.config.resolve())]
    with (state / 'server.stdout.log').open('wb') as stdout, (state / 'server.stderr.log').open('wb') as stderr:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                   close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
    print(process.pid)


if __name__ == '__main__':
    main()
