#!/bin/bash
# 打开配对窗口，显示一次性配对码（10 分钟内有效），管理端用它接入，不用拷 connection.json。
# Opens a pairing window and shows a one-time code (valid 10 minutes) for the orchestrator (macOS; on Windows use pair.ps1).
#   bash pair.sh [--config <config.json>]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
config="${AGENT_FLEET_CONFIG:-$HOME/Library/Application Support/agent-fleet/config.json}"
rest=()
while [ $# -gt 0 ]; do
  case "$1" in
    --config) config="$2"; shift 2;;
    *) rest+=("$1"); shift;;
  esac
done
python="$(dirname "$config")/venv/bin/python"
exec "$python" "$here/tools/pairing.py" --config "$config"
