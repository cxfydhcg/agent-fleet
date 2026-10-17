#!/bin/bash
# 把这台 Mac 接入 agent fleet，成为一台被管理的电脑（worker）。Windows 用 install-worker.ps1。
# Sets up this Mac as a managed computer (worker) in the agent fleet. On Windows use install-worker.ps1.
# 用平时登录的用户运行，不要 sudo / Run as your normal user, without sudo:
#   bash install-worker.sh [--connectivity local|tailscale|lan] [--full-access] [--language zh|en] [--manager-name 名字]
#   bash install-worker.sh --help        所有选项 / all options
# 可以重复运行：令牌、主 Agent 的 ID 和说明文件、已有数据都会保留。Safe to rerun.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"
for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    exec "$candidate" "$here/worker/install_mac.py" "$@"
  fi
done
if command -v brew >/dev/null 2>&1; then
  echo "==> Installing Python 3.12 with Homebrew / 用 Homebrew 安装 Python 3.12"
  brew install python@3.12
  exec "$(brew --prefix)/bin/python3.12" "$here/worker/install_mac.py" "$@"
fi
echo "Python 3.10 or newer is required: install it from https://www.python.org/downloads/ (or Homebrew), then rerun." >&2
echo "需要 Python 3.10 或更新版本：从 https://www.python.org/downloads/ 安装（或用 Homebrew），然后重新运行。" >&2
exit 1
