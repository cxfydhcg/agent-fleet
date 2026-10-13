---
name: setup-worker
description: Set up this Windows computer as an Agent Fleet worker with a main agent running Codex or Claude Code, choose local or Tailscale connectivity, or repair an existing worker installation.
---

# Set up the worker and main agent

Resolve the plugin root from this skill's location (`../..`), or use
`${CLAUDE_PLUGIN_ROOT}` in Claude Code. The installer is
`scripts/setup-worker.ps1`; it carries its runtime and needs no separate clone.
Use Windows PowerShell with `-NoProfile -ExecutionPolicy Bypass -File`.

Respect the user's existing engine, workspace and connectivity choices. For a new
installation, use Codex, local-only connectivity and restricted permissions unless
the user requests something else. Both Codex and Claude Code can invoke this
plugin; `-Engine codex|claude` selects the worker's actual execution engine.

1. Confirm this is the computer the user wants to operate as a worker if not
   already clear. Resolve the engine and local/Tailscale choice from their request.
2. Inspect the installation with `-Plan`. It prints only non-secret settings.
   Existing installations are reused. To change one, preserve its workspace,
   data directory, engine and network choices except for the requested change,
   then add `-Reconfigure`; the script refuses to interrupt active tasks.
3. Run setup with the chosen options, for example:
   `powershell -NoProfile -ExecutionPolicy Bypass -File "<plugin-root>/scripts/setup-worker.ps1" -Engine claude -Connectivity Local`.
   Optional arguments include `-Workspace`, `-DataDir`, `-Name`, `-Description`,
   `-NoAutostart`, `-SkipPrerequisites`, and `-ClaudeExecutable`.
4. Default Codex mode uses workspace-write. Default Claude Code mode requires
   version 2.1.248+ and uses `--restricted`, file tools, and `dontAsk`: file access
   stays in working directories, edits are approved there, and shell/code
   execution is unavailable. This is a CLI permission boundary, not an OS sandbox.
   Before passing `-FullAccess`, explain that it removes these restrictions and
   auto-approves agent permissions, including shell, desktop and browser actions;
   use it only if the user has explicitly agreed. Installing the plugin alone
   does not authorize full access.
5. The owner must sign in personally: `codex login` or `claude auth login`, and
   Tailscale when remote mode is selected. Never create an API key or print login
   credentials. If sign-in is missing, finish available setup and state the
   required human action. After sign-in, rerun setup; don't repeatedly attempt
   an unauthenticated model request.
6. Use the main-agent skill to send a harmless `OK` check after sign-in. Inspect
   completed/failed status, not just HTTP health. Report engine, computer name,
   network mode, workspace, permission mode and dashboard URL.

The permanent runtime is under the chosen workspace, outside the plugin cache.
Private service configuration, logs, registry and connection file are under the
reported data directory. Token files and connection contents must never enter
chat, source control, exports or the plugin package. Tell the user to privately
transfer `connection.json` to their orchestrator when connecting computers.

The service queues one task at a time. Do not open its live Codex sessions in the
desktop app; inspect progress using the dashboard. Claude sessions use the same
dashboard and can later be resumed with the Claude CLI.
