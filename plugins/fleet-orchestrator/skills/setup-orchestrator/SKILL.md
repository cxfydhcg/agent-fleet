---
name: setup-orchestrator
description: Set up this computer to manage an Agent Fleet from Codex or Claude Code, register a worker connection file, or connect this computer's local worker to the fleet.
---

# Set up the orchestrator

Resolve the plugin root from this skill's location (`../..`). Use its bundled
`scripts/setup-orchestrator.mjs`; do not depend on a checkout outside the plugin.
In Claude Code `${CLAUDE_PLUGIN_ROOT}` also identifies the plugin root.

1. Check Node.js (18 or newer). If missing, install it using the platform's normal
   installer within the user's requested setup; Windows supports
   `winget install --id OpenJS.NodeJS.LTS --exact`. Refresh the shell PATH afterward.
2. Run `node "<plugin-root>/scripts/setup-orchestrator.mjs"` to initialize the
   private registry. `AGENT_FLEET_HOME` selects an existing registry location;
   otherwise it uses `~/.agent-fleet`. Preserve existing workers and tokens.
3. If the user supplied a worker's `connection.json`, add it with
   `--connection "<absolute-file-path>"`. Add `--local` only when that worker is
   this computer. A local-only worker cannot be reached from another computer;
   use Tailscale setup there first. The script reports paths, never token values.
4. Run the bundled `skills/call-agent/fleet.mjs overview`. After registering a
   worker, send its main agent: `Installation check: reply with exactly OK. Do not
   use tools or dispatch child tasks.` Report the actual status and response.

If no connection file is available, explain that the orchestrator is initialized
and needs a worker registration. The Fleet Worker plugin creates connection files.
Let the user transfer them privately; never paste their contents into chat.

Use the call-agent skill for subsequent goals, progress, and notifications.
This setup does not start a worker or change the current computer's permission
mode. Do not log in, change accounts, publish, or delete connection files for the user.
