---
name: start
description: Start this computer's existing Agent Fleet worker service and check that it is ready. Use for /start, "启动 Agent Fleet 服务", "服务停了", or starting the service after closing an app or restarting the computer. Use setup for first installation or configuration changes, and call-agent for a service on another computer.
---

# Start this computer's service

Reply in the user's language. Resolve the plugin root from this skill's directory (`../..`), or use
`${CLAUDE_PLUGIN_ROOT}` in Claude Code.

Run:

```text
node "<plugin-root>/scripts/start.mjs"
```

The helper reads the existing worker config, calls the installed runtime's startup script, and checks
authenticated service health. It uses the configured workspace, port, connectivity and permissions;
an already-running service is reused. It does not rerun setup, stop tasks, open a pairing window, or
open a live agent conversation. The app does not need to be open for the background service to run.

The config defaults to `~/Library/Application Support/agent-fleet/config.json` on macOS or
`%USERPROFILE%\AppData\Local\agent-fleet\config.json` on Windows. `AGENT_FLEET_CONFIG` overrides it; if the user
installed elsewhere, pass `--config "<actual config.json path>"`.

Report `status: ready` and the returned dashboard URL after success. This proves the service is
reachable, not that a model turn completed. Do not send a task unless the user also asks to test an
agent. If config, runtime or prerequisites are missing, explain the reported problem and use setup
for repair within the user's requested scope. Do not repeatedly retry a failed launch.

Never print tokens, connection-file contents, full configuration, or raw logs containing credentials.
