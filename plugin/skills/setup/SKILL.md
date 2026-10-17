---
name: setup
description: Set up Agent Fleet on this computer in Codex or Claude Code - to control agents on other computers, to be controlled by another computer, or both. Also use it to register a worker's connection file, rename or reconfigure this computer's main agent, change its role, or repair an existing installation.
---

# Set up Agent Fleet

This one plugin contains both roles. Installing it does not choose a role or
start a worker. Resolve its root from this skill's directory (`../..`), or use
`${CLAUDE_PLUGIN_ROOT}` in Claude Code. All setup goes through the bundled
`scripts/setup.mjs`; do not depend on a checkout outside the plugin.

## 1. Ask how this computer will be used

If the request already says, use it. Otherwise ask in plain words, and don't
infer an answer merely because the plugin was installed:

- **Control other agents** (orchestrator): this computer sends goals to agents
  running on other computers.
- **Be controlled** (worker): this computer runs a main agent that receives and
  carries out tasks sent from another computer.
- **Both**: this computer controls other agents and also runs its own main agent
  for fleet tasks.

## 2. Check Node.js

Node.js 18+ is required. Bootstrap it if missing within the authorized setup
scope (Windows: `winget install --id OpenJS.NodeJS.LTS --exact`), then refresh PATH.

## 3. Resolve the settings

For every role, the **language** of everything people see (session titles, sidebar folders, the main agent's
instructions and replies, notices, the dashboard, installer and client messages): `--language zh|en`. Ask
during a fresh setup if the user's language is unclear; otherwise follow the language they write in. Existing
workers keep theirs; changing it on a worker needs `--reconfigure`.

For **control** (orchestrator):
- The private registry is `~/.agent-fleet` unless `AGENT_FLEET_HOME` or
  `--registry-home` names an existing one. Preserve existing workers and tokens.
- If the user has a worker's `connection.json`, pass `--connection PATH`. Add
  `--local` only when that worker is this computer. A local-only worker cannot be
  reached from another computer; set it up with Tailscale or LAN there first.

For **be controlled** or **both** (worker, Windows only):
- Respect an existing installation's engine, workspace, data directory and
  network choices. A fresh worker defaults to Codex, local-only connectivity and
  restricted permissions unless the user asks otherwise. `--engine codex|claude`
  picks the worker's engine; the current app can manage either one.
- Offer a main-agent name during fresh setup unless the user gave one, e.g.
  `--main-agent-name "Main Agent Work"` (1-100 printable characters; spaces and
  Unicode are fine). `--name` identifies the computer, not the agent. Preserve the
  name on reruns. This is the session title the user sees in the app; the user may also rename the session
  in the app at any time, and the dispatcher keeps that title. To change the configured name, rerun with the existing settings plus
  `--main-agent-name "<new name>" --reconfigure`; this keeps the conversation and
  workspace and refuses to overwrite another agent's name. Remote orchestrators
  must then import the updated connection file again.
- Optionally offer a **management name** for the main agent, `--manager-name "经理cris"`: what the user and
  the orchestrator call this computer's main agent when managing the fleet. It must be unique across the
  fleet and can be used wherever a computer is expected (e.g. `【经理cris】…`). It can be set or changed later
  from the orchestrator with `fleet.mjs set-manager-name <computer> <name>`.
- Every main agent gets a permanent id at its first setup (kept on reruns). The orchestrator maps the
  computer to that id, so renaming the main agent, changing its management name, or renaming the computer
  (same id in a new connection file) never loses the link.
- To change any other setting of an existing worker, keep everything else and add
  `--reconfigure`; the script refuses to interrupt active tasks.
- Permissions: default Codex mode uses workspace-write. Default Claude Code mode
  needs version 2.1.248+ and uses `--restricted`, file tools and `dontAsk`: file
  access stays in working directories and shell/code execution is unavailable.
  This is a CLI permission boundary, not an OS sandbox. `--full-access` removes
  these restrictions and auto-approves agent permissions, including shell,
  desktop and browser actions. Explain that and pass it only with the user's
  explicit agreement; installing the plugin does not authorize it.
- Connectivity: `local` (default) listens on 127.0.0.1 only. `tailscale` is
  reachable from the user's Tailscale devices, encrypted. `lan` listens on the
  local network at the auto-detected IPv4 address (or `--lan-address`) so
  computers on the same network can connect without Tailscale. LAN traffic is
  plain HTTP: anyone on that network who captures it can read the token, so
  explain this and use `lan` only on a network the user trusts. Every request
  still needs the token. The owner must allow inbound TCP on the worker port
  for Private networks in Windows Firewall as administrator, e.g.
  `New-NetFirewallRule -DisplayName "Agent Fleet worker" -Direction Inbound
  -Protocol TCP -LocalPort 8765 -Profile Private -RemoteAddress LocalSubnet
  -Action Allow`; never change firewall settings yourself. Suggest reserving
  the IP in the router; if it changes, rerun setup with `--reconfigure` and
  register the new connection file again.
- For **both**, setup registers this computer's worker with its own orchestrator
  over localhost; don't ask the user to transfer its connection file to itself.

Other options: `--workspace` (also the orchestrator's public project folder),
`--data-dir`, `--connectivity local|tailscale|lan`, `--lan-address`, `--description`,
`--claude-executable`, `--port`, `--tailscale-port`, `--skip-prerequisites`,
`--no-autostart`, and `--no-desktop` for headless or Claude Code-only use.

## 4. Preview, then run

Preview with `--plan`; it prints only non-secret settings. Run the same command
without `--plan` once the settings are resolved:

- `node "<plugin-root>/scripts/setup.mjs" --role orchestrator`
- `node "<plugin-root>/scripts/setup.mjs" --role orchestrator --connection "<path>"`
- `node "<plugin-root>/scripts/setup.mjs" --role worker --engine codex`
- `node "<plugin-root>/scripts/setup.mjs" --role both --engine claude`

To register another worker later, rerun with `--role orchestrator --connection`
(or use call-agent's `add-computer`).

## 5. Sign in and verify

- The owner signs in personally when needed: `codex login`, `claude auth login`,
  or Tailscale for remote mode. Never create an API key or print credentials. If
  sign-in is missing, finish what setup can do, state the required human action,
  and rerun setup after sign-in rather than retrying unauthenticated requests.
- Send each new or changed worker's main agent, via main-agent (this computer) or
  call-agent (another computer): `Installation check: reply with exactly OK. Do
  not use tools or dispatch child tasks.` Report the actual status and reply;
  service-ready or HTTP health is not proof of a successful model turn.
- For an orchestrator, run call-agent's `fleet.mjs overview`. With no workers
  registered, explain that the orchestrator is ready and needs a worker's
  connection file, which a worker setup on the other computer creates. The user
  transfers it privately; never paste its contents into chat.
- Report role, engine, computer name, connectivity, permission mode, workspace,
  data directory and, for a worker, its dashboard URL
  (`http://127.0.0.1:8765/dashboard`).

## 6. Desktop app visibility

- Setup registers the workspace under Projects in Codex desktop on Windows and
  reports `desktop.status`, including when it reuses an existing worker. Verify
  `registered` and the path with the app's project-list tool when available;
  never report a sidebar project solely because its directory exists. `pending`
  needs the owner's response to any folder-access prompt and another setup run.
  Never edit the running app's internal state file to register a project.
- For a Codex worker, the verification reply includes its main-agent `thread_id`.
  When Codex app tools are available, read that thread without navigating to it,
  pin it with the app's sidebar tool, and verify its configured name in the
  pinned list. Adding the project folder alone does not make a worker-created
  chat visible. Never open the live session or create a replacement chat. If app
  tools are unavailable, say that sidebar visibility wasn't verified. Claude
  worker conversations are viewed through the dashboard.

## 7. Session title

End the final report by reminding the user that this session's title still
reflects setup, and offer to rename it after the chosen role (for example
"orchestrator agent work", "worker agent work" or "orchestrator + worker agent
work"). If the user already named a title in the request, rename it without
asking. Otherwise rename only after they agree, using the app's session-rename
tool when one is available.

## Data and safety

Keep worker and orchestrator data separate while preserving existing registries
and instructions. The worker's permanent runtime lives under its workspace,
outside the plugin cache; private service config, logs, registry and connection
file live under the reported data directory. Switching to orchestrator-only does
not uninstall or stop an existing worker; remove one only if the user asks. The
service queues one task at a time. No token, connection contents or login
credentials belong in chat, source control, exports or the plugin package. Do
not log in, change accounts, publish, or delete connection files for the user.

Use main-agent for this computer's work and call-agent for any registered fleet
computer.
