---
name: setup
description: Set up Agent Fleet on this computer in Codex or Claude Code - to control agents on other computers, to be controlled by another computer, or both. Also use it to register a worker's connection file, rename or reconfigure this computer's main agent, change its role, or repair an existing installation.
---

# Set up Agent Fleet

This one plugin contains both roles. Installing it does not choose a role or
start a worker. Resolve its root from this skill's directory (`../..`), or use
`${CLAUDE_PLUGIN_ROOT}` in Claude Code. All setup goes through the bundled
`scripts/setup.mjs`; do not depend on a checkout outside the plugin.

## How to ask

Every setup choice (language, role, permission mode, connectivity) is a multiple-choice question. When
the app offers a choice tool, ask through it so the user clicks an option instead of typing: in Claude
Code that is `AskUserQuestion`; in Codex, its question tool (such as `request_user_input`) when available. One question per
decision, each option a short label with a one-line description, the recommended option first and
marked "(推荐)" or "(Recommended)". The tool adds a free-text "Other" itself; don't add one. Once the
language is known, role, permission mode and connectivity can go in one call as separate questions,
asked only for the ones the request hasn't already settled. Without a choice tool, ask in plain text
and list the same options. Never infer an answer the user hasn't given.

## 1. Settle the language first

Before anything else, decide the language, and use it for the whole setup conversation and as
`--language zh|en`:

- If the user writes in Chinese, use Chinese (`zh`); if in English, English (`en`). Say which one you
  picked in one short line and that they can switch. These instructions are in English; that is not a
  reason to answer in English.
- If it is unclear (e.g. only a slash command or a button), ask this one question first, in both
  languages: "中文还是 English？/ Chinese or English?", as a choice with the options `中文` and
  `English`. Ask nothing else until it is answered.
- An existing worker keeps its language unless the user asks to change it (`--reconfigure`).

The setting only decides fixed text people see (default session titles and folder names, the dashboard,
notices, installer and client messages, the main agent's instruction file). The models understand both
languages either way, and agents reply in the language a person writes to them in.

## 2. Ask how this computer will be used

If the request already says, use it. Otherwise ask, as a choice (see How to ask), and don't
infer an answer merely because the plugin was installed:

- **Control other agents** (orchestrator): this computer sends goals to agents
  running on other computers.
- **Be controlled** (worker): this computer runs a main agent that receives and
  carries out tasks sent from another computer.
- **Both**: this computer controls other agents and also runs its own main agent
  for fleet tasks.

For **be controlled** or **both**, settle the permission mode in the same exchange, so the worker is
installed with it rather than changed later. Unless the request already says, ask it as a choice:

- **Full access** (`--full-access`): sub-tasks can operate the desktop, browsers and other apps
  (Computer Use), run any command, and every permission request is approved automatically. They can
  also do anything else on the machine.
- **Restricted** (default): sub-tasks only write their own working folder; anything that needs
  approval, including Computer Use on any app, is refused.

Recommend full access when the user wants agents to control apps or the desktop. Pass `--full-access`
only after the user picks it; installing the plugin does not authorize it. Worker setup refuses to run
without `--full-access` or `--restricted`, so this question is never skipped. On a rerun, first show the
current mode (`full_access` in the `--plan` output) and ask whether to keep it.

## 3. Check Node.js

Node.js 18+ is required. Bootstrap it if missing within the authorized setup
scope (Windows: `winget install --id OpenJS.NodeJS.LTS --exact`), then refresh PATH.

## 4. Resolve the settings

Pass the language from step 1 as `--language zh|en` for every role.

For a fresh **worker** or **both** setup, unless the user already chose connectivity, ask how this
computer should be reachable, as a choice that always shows all three options together:

- **Only this computer / 仅本机** (`local`, default): listen on loopback only; no other computer can connect.
- **Trusted local network / 可信局域网** (`lan`): other computers on that network can connect; HTTP is unencrypted.
- **Tailscale encrypted connection / Tailscale 加密连接** (`tailscale`): connect from the user's Tailscale devices.

Do not restrict the question to "how other computers connect" or omit `local` because the role is
worker. On reruns preserve the configured connectivity unless the user asks to change it. Choosing
local needs no remote pairing; report that the worker is usable on this computer only.

For **control** (orchestrator):
- The orchestrator is a session, not a folder: setup registers the session running it (its
  `CLAUDE_CODE_SESSION_ID` / `CODEX_THREAD_ID`, or `--session ID`) and creates no workspace or Codex desktop
  project. Only that session reaches main agents and pairs computers; `fleet.mjs claim-orchestrator` moves the
  role when the user asks to use another conversation. Report the registered session id.
- The private registry is `~/.agent-fleet` unless `AGENT_FLEET_HOME` or
  `--registry-home` names an existing one. Preserve existing workers and tokens.
- To connect a worker on another computer, prefer pairing over copying files: the worker shows an 8-digit
  code at the end of its setup (`pairing.code` in the setup result; `pair.ps1` in its `service\worker`
  folder makes a new one). On the orchestrator run `fleet.mjs discover`, then `fleet.mjs pair <code>` (or
  `pair <IP> <code>` when the search finds nothing). Tell the user the code and how long it is valid; never
  pair with a code the user did not give you. Pairing later (expired code, another computer) is the pair
  skill's job; point the user to it instead of rerunning setup.
- If the user has a worker's `connection.json`, pass `--connection PATH`. Add
  `--local` only when that worker is this computer. A local-only worker cannot be
  reached from another computer; set it up with Tailscale or LAN there first.

For **be controlled** or **both** (worker, Windows or macOS; the same options work on both. On macOS
missing Node.js/Tailscale come from Homebrew, data lives in `~/Library/Application Support/agent-fleet`, and
autostart is a LaunchAgent. With `--full-access` the owner must grant Accessibility and Screen Recording to
Codex/Terminal in System Settings → Privacy & Security; you cannot do that for them):
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
  explicit agreement (step 2); installing the plugin does not authorize it.
- Changing the permission mode of an existing worker: rerun setup with `--full-access --reconfigure`
  (or `--restricted --reconfigure` to switch back). The
  service restarts only when idle; if it was busy, rerun after its tasks finish. Verify the result with
  call-agent's `fleet.mjs overview`, whose `permissions` field shows what the running service uses;
  the installer's "Sub-task permissions" line alone is not proof that the service restarted.
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

Other options: `--workspace` (the worker's folder; the orchestrator has none),
`--data-dir`, `--connectivity local|tailscale|lan`, `--lan-address`, `--description`,
`--claude-executable`, `--port`, `--tailscale-port`, `--skip-prerequisites`,
`--no-autostart`, and `--no-desktop` for headless or Claude Code-only use.

## 5. Preview, then run

Preview with `--plan`; it prints only non-secret settings. Run the same command
without `--plan` once the settings are resolved:

- `node "<plugin-root>/scripts/setup.mjs" --role orchestrator`
- `node "<plugin-root>/scripts/setup.mjs" --role orchestrator --connection "<path>"`
- `node "<plugin-root>/scripts/setup.mjs" --role worker --engine codex --full-access`
- `node "<plugin-root>/scripts/setup.mjs" --role both --engine claude --restricted`

To register another worker later, rerun with `--role orchestrator --connection`
(or use call-agent's `add-computer`).

## 6. Sign in and verify

- The owner signs in personally when needed: `codex login`, `claude auth login`,
  or Tailscale for remote mode. Never create an API key or print credentials. If
  sign-in is missing, finish what setup can do, state the required human action,
  and rerun setup after sign-in rather than retrying unauthenticated requests.
- Check each new or changed worker's main agent with `fleet.mjs check <computer>`,
  through main-agent's client (this computer) or call-agent's (another computer).
  It sends a fixed "reply with exactly OK" message straight to the main agent
  from any session; don't use `export` for this, which from a session other than
  the orchestrator session goes to that session's own agent instead. Report the actual
  status and reply; service-ready or HTTP health is not proof of a successful model turn.
- For an orchestrator, run call-agent's `fleet.mjs overview`. With no workers
  registered, explain that the orchestrator is ready and needs a worker's
  connection file, which a worker setup on the other computer creates. The user
  transfers it privately; never paste its contents into chat.
- Report role, engine, computer name, connectivity, permission mode, workspace,
  data directory and, for a worker, its dashboard URL
  (`http://127.0.0.1:8765/dashboard`).

## 7. Desktop app visibility

- Codex workers register both projects through the app. New installations use
  a real task directory named `AgentFleet-其他` (zh) or `AgentFleet-Other` (en),
  so the other project appears with the right name without restarting Codex.
  Reinstalls preserve the configured task path. Its basename must match the
  configured project name; mismatched configuration is rejected, not migrated.
  Respect an explicit `codex_app_register_other_project: false` opt-out, unless
  the user subsequently requests the correctly named other project.
  Check both entries in `desktop.projects` when enabled. A registered main
  project alone is not complete. Existing workers can repair only this step with
  `node "<plugin-root>/scripts/setup.mjs" --role worker --language zh --desktop-only`
  (use the configured language and `--data-dir` if custom). Preview with `--plan`.
  This does not restart the service or change permissions, identity or pairing.
  Verify both project registrations before claiming completion.
- A Claude worker (`--engine claude`) gets its folder in the Claude desktop app instead of Codex:
  setup opens `claude://code/new?folder=<workspace>`, which opens a Code draft there after the
  owner confirms the folder prompt; the other-agents folder (`tasks_dir`) is opened the same way, so
  expect two prompts. The Code sidebar lists sessions, not folders, so a draft appears there only
  after a message is sent in it. Claude desktop has no project list to read back, so
  `desktop.status` is `opened` (or `not-installed`), never `registered`; tell the owner to confirm
  the prompts and send a message in each draft, and don't claim more than that. Nothing is
  registered in Codex.
  When setup itself runs in the Claude desktop app and a session-starting tool is available (in
  Claude Code: `start_session` with `cwd`), offer to start one short session in each folder instead,
  so both appear in the sidebar without the owner typing. Each such session must only reply briefly
  and never change files there: the worker service uses them.
- A Codex worker's setup registers the workspace under Projects in Codex desktop on Windows and macOS (the
  orchestrator registers none) and
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

## 8. Session title

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
