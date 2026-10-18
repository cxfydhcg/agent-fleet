[中文](README.md) | English

# agent-fleet

![agent-fleet overview](docs/overview.en.svg)

One AI agent directs AI agents on many computers. It doesn't build a new agent: it adds communication, scheduling and management across computers on top of Codex or Claude Code. Each computer can use either engine or both, and you can mix them.

```
You (phone / computer)
  └→ the orchestrator's agent (Codex or Claude Code)
       └→ each computer's main agent (in charge of that computer)
            └→ sub-agents on that computer (do the work: desktop, browsers, apps)
```

- **The main agent decides**: the orchestrator gives a goal; the main agent does it itself, sends one sub-agent, or splits it up.
- **Finished sub-agents wake the main agent**, which keeps going; when the whole job is done it tells the orchestrator, which tells you.
- **Every computer has a profile**: hardware, purpose, software, account status, and its resident agents with their roles, so work goes to the right computer.
- **Fleet board**: watch every computer's progress live on one read-only page on the orchestrator, without disturbing the agents.

## Requirements

- Windows 10/11 or macOS
- Codex, Claude Code, or both on each computer. The orchestrator can use either; which one a managed computer works with is chosen during setup, independently of the one you set it up from
- The computers can reach each other: on the same local network, or all on [Tailscale](https://tailscale.com)

Everything else (Node.js, Python, the runtime) is handled during setup.

## Install

Orchestrators and managed computers install the same way: install the plugin, then let the AI set it up.

**1. Install the plugin** (whichever you use):

Codex: run in a terminal

```
codex plugin marketplace add cxfydhcg/agent-fleet
codex plugin add agent-fleet@agent-fleet
```

Claude Code: type in the chat box

```
/plugin marketplace add cxfydhcg/agent-fleet
/plugin install agent-fleet@agent-fleet
```

**2. Let the AI set it up**: open a new chat and say "Use Agent Fleet setup to set up this computer" (in Claude Code you can also use `/agent-fleet:setup`). It asks you step by step, roughly like this:

> **You**: Use Agent Fleet setup to set up this computer
>
> **AI**: Continuing in English. How will this computer be used? ① direct other computers (orchestrator) ② be directed by another computer (worker) ③ both
>
> **You**: Be directed; it's for video editing
>
> **AI**: How should this computer be reachable: only this computer (default), trusted local network, or Tailscale encrypted connection? Should sub-tasks be able to operate the desktop and browsers (full access)? Choose Codex, Claude Code, or both. What should each main agent be called, and do you want a management name (e.g. "Manager Cris")?
>
> **You**: Local network, full access, management name Manager Cris
>
> **AI**: Done. Pairing code `4821-7395`, valid 10 minutes. On the orchestrator, say "search for new computers and pair" and give it this code.

**3. Connect**: in the orchestrator's chat, say:

> Search for new computers and pair, code 4821-7395

It searches the local network and Tailscale; if it finds nothing, it asks for that computer's IP. The code works once, so others on the same network cannot connect without it.

To pair again later (code expired, a new computer, a changed IP), use the pair skill instead of rerunning setup: `/agent-fleet:pair` in Claude Code, `$` → pair in Codex, or just say "get a pairing code" / "search for new computers and pair". On a worker it shows a code; on an orchestrator it searches and connects.

Outgoing discovery and pairing require completed orchestrator (or both) setup and its configured
management session. A pure worker can only show its own code; receiving another computer's code does
not make it an orchestrator. Local-only workers do not need remote pairing.

**Permissions**:
- **Full access** needs your explicit agreement: sub-agents are no longer sandboxed and can operate the desktop, browsers and other programs, which means they can do anything on that computer. Without it, sub-agents can only read and write their own working folder.
- A Mac with full access asks you to allow Accessibility and Screen Recording in System Settings → Privacy & Security; you must click Allow yourself.
- When Codex directs other computers, its sandbox blocks network access by default and may ask whether to allow it; allow it.
- On Windows over a local network, the firewall port must be opened as administrator (the AI gives you the command; you run it yourself). On a Mac, click Allow if asked whether to accept incoming connections.

## Usage

To start this computer's installed worker, use `$agent-fleet:start` in Codex or
`/agent-fleet:start` in Claude Code, or ask to start the Agent Fleet service. It keeps the existing
configuration, checks service readiness, and reuses a running service without rerunning setup.

To update to the latest release, use `/agent-fleet:update` on the orchestrator (`$agent-fleet:update` in Codex), or say "update the plugin". It updates this computer's plugin and worker, then every other computer (one short task each; it waits for the service to restart and reports the old and new version). Computers in restricted mode cannot be updated remotely; run it on that computer.

On the orchestrator, just say things like:

> Have 【Manager Cris】 edit today's video and put it in the shared folder

> List all computers and agents

> Find a computer with a GPU to edit a video

> Ask 【Manager Cris】 how the last task went

> Stop 【Manager Cris】 from starting new tasks

Sub-agents' results go into `AgentFleet-Other/<task>/` or `shared/` in that computer's workspace (see "Where files live" below). To see images or videos another computer produced, use the fleet board's Files tab, or ask the AI to bring them over with `fleet.mjs fetch`. To watch progress, ask the AI on the orchestrator to open the fleet board (`fleet.mjs dashboard`): one page with every computer's tasks and sessions, no token to paste. Sessions are split into the AgentFleet and AgentFleet-Other folders, and a conversation reads like Claude: formatted Markdown, with the tools each reply used folded under it, input and output one click away. Each computer also has its own read-only dashboard at `http://<that computer's address>:8765/dashboard` (port 8766 over Tailscale); the first time, paste that computer's token.

## Where files live

Each worker has two folders: the workspace (where agents work) and the data directory (settings, token, task records).

| | Windows | Mac |
|---|---|---|
| Workspace | `C:\Users\<user>\Documents\AgentFleet\` | `~/AgentFleet/` |
| Data directory | `C:\Users\<user>\AppData\Local\agent-fleet\` | `~/Library/Application Support/agent-fleet/` |

The workspace can be chosen with `--workspace` at first setup; reruns keep it.

```
AgentFleet\                    ← workspace, "AgentFleet" in the desktop app sidebar
├── main-codex\                ← where the Codex main agent works (AGENTS.md, .codex\config.toml); only with Codex
├── main-claude\               ← where the Claude main agent works (CLAUDE.md); only with Claude
├── AgentFleet-Other\          ← sub-task folders, "AgentFleet-Other" in the sidebar
│   └── <task name-task id>\   ← one folder per sub-task, with its output
├── shared\                    ← shared folder for exchanging files between agents
├── service\                   ← the worker service
│   ├── worker\                ← server.py, start/stop scripts, dashboard page
│   ├── install-worker.ps1
│   └── plugin-version.txt     ← plugin version currently deployed
└── worker-rules.md            ← rules for sub-agents: do only their task, never dispatch, never touch tokens
```

The data directory holds `config.json` (settings), `token.txt` and `connection.json` (access token; never share), `venv` (Python) and `state` (one record per task in `state/jobs/<task id>/`). A Claude-engine worker keeps no config file in the workspace: its settings and dispatcher connection are passed to `claude` as command-line arguments on every run. Claude desktop groups sessions by the folder they start in, so Claude-engine sessions start in one of these two folders (each still working in its own folder). Both engines group the same way: the main agent and the sub-tasks it opens under "AgentFleet", sessions other computers open directly under "AgentFleet-Other".

The fleet board's Files tab and `fleet.mjs files` / `fetch` reach only `shared` and `AgentFleet-Other`.

## Names, language and control

- **Three separate names**: the session title (what you see in the app; change it any time, the service won't change it back); the management name (what you call it when directing, e.g. "Manager Cris", unique in the fleet); and the main agent id (created at setup, never changes; the orchestrator finds the main agent by it, so renaming never breaks the link).
- **Language**: Chinese or English. It only affects fixed text (default names, the dashboard, notices, messages). The models understand both, and agents reply in the language you write in.
- **Session ownership**: a main agent can continue or cancel only the sessions it opened; the orchestrator controls them all. You can stop an agent from starting new tasks, or lock a resident agent so only the orchestrator controls it.
- **One name, one session**: a registered resident agent keeps a single session, so you never get several sessions with the same name.

## Things to know

- **The token is control of that computer.** Keep it on your own computers; don't send it to anyone or paste it into a chat.
- **Opening an agent's session in a desktop app**: fine in Codex and Claude. While you hold it, new tasks for that agent continue in a fork with the full history, so you are not interrupted; the Codex desktop app holds every session it opened until it fully quits (set `fork_when_busy: false` to fail instead). To just watch progress, use the read-only dashboard, which is always current.
- **Each engine runs one task at a time**. Both engines can run concurrently; main agents should coordinate work sharing the desktop.
- **Quota pauses affect only that engine**: its queue waits in order until reset and notifies the orchestrator; the other engine continues.
- Sub-agents are told not to touch the dispatcher or the token, but that is only a rule: with full access they can technically read any file on the computer.


## Two engines on one computer

Choose Codex, Claude Code, or both during setup, and give each main agent a name. One worker shares a service, port and token. Each engine runs one task at a time; the two engines can work concurrently and their quota pauses are independent.

```text
node <plugin-root>/scripts/setup.mjs --role worker --engines codex,claude --main-agent-name "Main agent" --second-main-agent-name "Claude main agent" --restricted --plan
```

Remove `--plan` to install. Windows PowerShell entries accept `-Engines codex,claude`, `-MainAgentName` and `-SecondMainAgentName`; macOS entries use the same `--` options shown above. `--engine codex` / `--engine claude` still selects a single engine. The owner must sign in to both CLIs for a dual installation.

Fresh dual installs create `main-codex/AGENTS.md` (with its own `.codex/config.toml`) and `main-claude/CLAUDE.md`. Single-engine installs keep `main-agent`. Reruns preserve each id, name, folder and instruction file. Defaults are “Main agent” and “Claude main agent” in English, “主agent export” and “Claude主agent” in Chinese.

From the orchestrator session, use `fleet.mjs agent <computer> "Claude main agent" "Organize files"`. The name selects the engine. `export <computer>` still selects the first main; `check <computer> [name]` checks one main, or every main when omitted. `overview` lists all engines and mains. Other sessions cannot gain main-agent rights by using a main's name.

Config adds `engines` and `main_agents` (each with `name`, `engine`, `id`, `workspace`). Legacy `executor`, `main_agent_name`, `main_agent_id`, `main_agent_workspaces` describe the first engine. Configs without `engines` retain single-engine behavior. Resumes keep the original session engine; children inherit their main's engine, and direct submissions can explicitly select an enabled `engine`. Sessions appear in the desktop app for the engine that actually ran them.

## Advanced settings

Edit the worker's `config.json` (Windows: `%USERPROFILE%\AppData\Local\agent-fleet\`, Mac: `~/Library/Application Support/agent-fleet/`) and restart the service.

| Key | Default | What it does |
|---|---|---|
| `quota_guard` | `true` | Checks the Codex 5-hour and weekly quota before each task; when used up, the queue waits for the reset |
| `quota_guard_threshold` | `100` | Percent used that counts as used up, e.g. `95` to keep a margin |
| `resident_title_suffix` | `" - resident"` | Session title suffix for resident agents (`" - 固定"` in Chinese installs); `""` turns it off |
| `codex_app_folders` | on | Files sessions into two Codex app sidebar folders (opened by the main agent / by other agents); sorts only while the app is closed; `false` turns it off |

To install without the AI, run the installer directly: `install-worker.ps1` on Windows, `install-worker.sh` on a Mac; see the notes at the top of each script or `--help`.

## Development

```
cd worker
python -m unittest test_dispatcher.py test_claude_executor.py
cd .. && python -m unittest tests/test_plugins.py
```

More conventions in [AGENTS.md](AGENTS.md).

## License

MIT
