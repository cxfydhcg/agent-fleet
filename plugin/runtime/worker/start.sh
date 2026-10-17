#!/bin/bash
# 启动这台 Mac 的 worker 服务。
# Starts this Mac's worker service (macOS; on Windows use start.ps1).
#   bash start.sh [--config <config.json>]
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
exec "$python" "$here/service_ctl.py" start --config "$config" "${rest[@]+"${rest[@]}"}"
