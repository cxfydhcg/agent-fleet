"""Open a pairing window on this worker and show the one-time code.

An orchestrator on another computer finds this worker (LAN search, Tailscale devices, or the address you
give it) and presents the code; it then gets the connection info without anyone copying connection.json.
The code is valid for 10 minutes, works once, and dies after 5 wrong tries.

    python pairing.py --config <dispatcher config.json> [--json]

Only the standard library is used.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--config', required=True, help="the dispatcher's config.json")
    parser.add_argument('--json', action='store_true', help='print the result as JSON (for setup scripts)')
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    english = config.get('language') == 'en'
    token = Path(config['token_file']).read_text(encoding='utf-8').strip()
    request = urllib.request.Request(f"http://127.0.0.1:{config.get('port', 8765)}/pair/start", data=b'{}', method='POST',
                                     headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read())
    except OSError as error:
        sys.exit(('The worker service is not answering: ' if english else '派任务服务没有响应：') + str(error))
    reachable = result.get('lan_url') or result.get('tailscale_url')
    if args.json:
        print(json.dumps({**result, 'reachable_from_other_computers': bool(reachable)}, ensure_ascii=False))
        return
    minutes = result['expires_in'] // 60
    if english:
        print(f"Pairing code: {result['code']}  (valid {minutes} minutes, works once)")
        print(f"This computer: {config.get('computer_name')}  {reachable or '(local only)'}")
        print('On the orchestrator, ask Claude to "search for workers and pair", and give it this code.')
        if not reachable:
            print('This worker listens on this computer only; other computers cannot pair. Set it up with LAN or Tailscale first.')
    else:
        print(f"配对码：{result['code']}（{minutes} 分钟内有效，只能用一次）")
        print(f"这台电脑：{config.get('computer_name')}  {reachable or '（只能本机访问）'}")
        print('在管理端电脑上对 Claude 说"搜索并配对新电脑"，再把这个配对码告诉它。')
        if not reachable:
            print('这台 worker 只在本机监听，别的电脑配不上。先用 LAN 或 Tailscale 方式重新安装。')


if __name__ == '__main__':
    main()
