[中文](README.md) | English

# agent-fleet

**Agent Fleet is a plugin** you can install into Codex or Claude Code. After installing, run setup and choose this computer's role: orchestrator, worker or both; a worker can use either the Codex or the Claude Code engine. See [PLUGINS.md](PLUGINS.md) for installation, permissions and verification.

Use one AI agent to manage AI agents on many computers. It does not build a new agent; it adds a layer of cross-computer communication, scheduling and management on top of the existing Codex.

```
You (phone / computer)
  └→ orchestrator agent (Claude Code, on the orchestrator computer)
       └→ each computer's main agent (Codex, in charge of that computer)
            └→ sub-agents on that computer (Codex, do the actual work: operate the desktop, browsers, apps)
```

- **The main agent has autonomy** (default name `Main agent` in English installs, `主agent export` in Chinese ones): the orchestrator only gives it a goal, and it decides whether to do the work itself, send one sub-agent, or split it across several.
- **A sub-agent automatically wakes the main agent when it finishes**, and the main agent continues from there; when the whole job is done, it notifies the orchestrator, which then tells you.
- **Every computer has a profile**: hardware (detected once automatically), purpose, software, account sign-in status, and the resident agents and their responsibilities.
- **Read-only dashboard**: watch each computer's sessions and progress live in a browser or on your phone, without disturbing the agents at work.

## Components

| Folder / file | Installed on | Purpose |
|---|---|---|
| `worker/` | each managed computer | the dispatcher (MCP over HTTP), the read-only dashboard, and the main agent's instruction template |
| `install-worker.ps1` | each managed computer | one-step worker install |
| `plugin/` | orchestrator computer | Claude Code plugin: the `/call-agent` skill and its bundled `fleet.mjs` command-line client |
| `install-orchestrator.ps1` | orchestrator computer | installs the skill manually when you don't use the plugin marketplace (can also install it into Codex) |
| `AGENTS.md` / `CLAUDE.md` | — | install and development instructions for AI agents |

Each managed computer has a workspace, by default `%USERPROFILE%\AgentFleet\`; change it with `-Workspace`:

```
AgentFleet\
  main-agent\    the main agent's working directory
  tasks\         one folder per sub-task: date_taskname_number
  shared\        files handed between sub-tasks, and final output for you
```

Internal data (config, tokens, task records, profiles, notifications) lives in `%LOCALAPPDATA%\agent-fleet\`; the orchestrator's registry and the per-computer tokens live in `~/.agent-fleet/`. None of this is **in** the project folder, so none of it goes into git.

## Requirements

- Windows 10/11 (only tested on Windows so far; Mac needs its own startup and hardware-detection parts)
- [Tailscale](https://tailscale.com): all computers sign in to the same account to form a private network
- Codex: signed in with your ChatGPT account (`codex login`)
- The orchestrator needs [Claude Code](https://claude.com/claude-code) and Node.js

## The easy way: let an AI install it

After cloning this project, open Claude Code or Codex in the project folder and say:

> Set this computer up as an agent-fleet worker (or orchestrator).

The AI follows the steps in `AGENTS.md` and stops to remind you at the points only you can do, such as signing in to Tailscale and to Codex. The manual steps follow.

## Install a managed computer

On that computer, as the Windows user you normally sign in with (no administrator needed):

```powershell
git clone <project URL> agent-fleet
cd agent-fleet
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-worker.ps1 -FullAccess -Description "Video editing machine with a GPU"
```

The script installs any missing Node.js, Python, Tailscale and Codex CLI, deploys the dispatcher, generates a token, creates this computer's main agent, sets up autostart at logon, and finally writes `%LOCALAPPDATA%\agent-fleet\connection.json`.

Only two things need a person: **signing in to Tailscale** and **signing in to Codex**. If either is missing, the script reminds you; sign in and run it again.

`-FullAccess`: sub-agents run without a sandbox and permission requests are approved automatically, which they need to operate the desktop, browsers and other programs. Without it, sub-agents can only read and write their own working directory.

## Install the orchestrator

On the computer that runs Claude Code, install from the plugin marketplace (recommended):

```
/plugin marketplace add cxfydhcg/agent-fleet
/plugin install agent-fleet@agent-fleet
```

Once installed, the skill is called `/agent-fleet:call-agent`. You can also skip the marketplace: clone the project and run `install-orchestrator.ps1` to install it as `/call-agent`; add `-Codex` to also install it into Codex, so Codex can use the same skill.

Then connect each managed computer. You can ask Claude to run this for you, or run the client in the skill folder yourself:

```powershell
node <skill folder>\fleet.mjs add-computer <that computer's connection.json>
```

After connecting, you can delete `connection.json`; the token is already saved in `~/.agent-fleet/tokens/`. If the orchestrator is itself a managed computer, add `--local=1` so it uses the local address.

## Usage

In Claude Code:

```
/agent-fleet:call-agent 【pc-1】【Main agent】check whether there are new Bilibili DMs
/agent-fleet:call-agent 【pc-1】              list the agents on this computer
```

(With a manual install the command is `/call-agent`.) You can also just say "list all computers and agents" or "find a computer with a GPU to edit a video".

Common commands (`node <skill folder>\fleet.mjs …`):

| Command | Purpose |
|---|---|
| `overview` | profile overview of all computers |
| `agents <computer>` | the agents you can talk to on this computer and their responsibilities |
| `agent <computer> "<agent name>" "<message>"` | talk to an agent (continues its session and waits for the reply) |
| `watch` | continuously print new notifications from all computers (for background monitoring) |
| `notifications <computer>` | read notifications in full |
| `profile <computer> [--refresh]` | a computer's full profile; `--refresh` re-detects hardware |
| `describe-agent <computer> "<agent name>" --role=responsibility [--can-dispatch=on\|off] [--locked=on\|off]` | register an agent; `--can-dispatch=off` stops it from opening new sessions, `--locked=on` means only the orchestrator can continue/cancel/open its sessions |

**Session ownership**: sessions a main agent opens with `submit_task` belong to it, and `continue_task` / `cancel_task` / `open_task` only work on sessions it opened itself (and their continuations); the orchestrator can control all sessions. Only the orchestrator can set `can_dispatch` and `locked`, and a main agent cannot remove a registration that carries these restrictions. The restrictions apply to main-agent calls the dispatcher can identify (the Claude engine through a per-turn request header, Codex through turn metadata); requests whose caller cannot be identified are treated as the orchestrator. They are management rules, not a security boundary: anyone holding the token still has full control.

**Sessions with the same name**: a registered agent (resident or main) keeps only one session. Once it has a session, another `submit_task` with that name is rejected, and the error includes the `task_id` to continue (each agent in `get_profile` also has a `task_id`). A new one can be opened only after the user archives the old session in the Codex desktop app. Unregistered one-off tasks may share names.

**Names and language**: each computer's main agent has three separate things:

| | What it is | Who changes it |
|---|---|---|
| Session title | The name shown in the Codex desktop app (`-MainAgentName` at setup; default `Main agent`, or `主agent export` in Chinese installs) | The user, any time, in the app; the dispatcher does not change it back. It sets the title again only when the title it wants changes (e.g. the agent was just registered as resident) |
| Management name | What people and the orchestrator call it when managing the fleet, e.g. `Manager Cris`; unique across the fleet and usable wherever `<computer>` is expected | `fleet.mjs set-manager-name <computer> <name>`, or `-ManagerName` at setup |
| Main agent id | Generated at the first setup and never changed | Nobody. The orchestrator finds the main agent by it, so renaming the title, the management name or even the computer never loses the link |

The language (`-Language zh|en`, or `--language` in plugin setup) sets every text people see: default session titles and folder names, the resident suffix, the main agent's instructions and reply language, automatic notices, the dashboard, and installer and client messages. Existing computers keep their language (earlier installs are Chinese). The orchestrator client's language is set with `fleet.mjs language zh|en` and follows the system by default.

Read-only dashboard: `http://<computer's Tailscale name>:8766/dashboard`; paste that computer's token the first time you open it.

## Things you must know

- **The token is control of the computer**. With `-FullAccess`, anyone who holds the token and can reach the service can make the computer do anything. Keep tokens only on your own computers and never send them to anyone; don't share your Tailscale account either.
- **Don't open a session an agent is using in the Codex desktop app**. The app keeps every session it has opened locked until Codex fully exits, and during that time the agent cannot continue that session. To watch progress, use the read-only dashboard.
- **The dispatcher runs only one task at a time**, so tasks don't fight over the mouse and keyboard. After dispatching, the main agent ends its turn and waits for the automatic notice that the sub-agent has finished.
- **No task starts when the Codex quota is used up**. Before each task starts, the dispatcher checks the Codex 5-hour and weekly quotas. If either is used up, the whole queue pauses in its original order until about 1 minute after the quota resets (it waits at most 6 hours before checking again). The task's latest message and the dashboard show the expected time, and a notification is sent to the orchestrator. If the check fails or returns a format it can't understand, tasks run as usual.
- **Sub-agents are explicitly told not to touch the dispatcher or tokens**. This is a rule written for them, not a technical restriction: under `-FullAccess` a sub-agent can read any file on the computer.

## Optional setting: Codex quota guard

Edit `%LOCALAPPDATA%\agent-fleet\config.json`; it takes effect after restarting the dispatcher. It only applies to the default app-server mode.

| Key | Default | Purpose |
|---|---|---|
| `quota_guard` | `true` | Check the Codex quota before a task starts (app-server's `account/rateLimits/read`); when it is used up, the queue waits for the reset. Set to `false` to turn it off. |
| `quota_guard_threshold` | `100` | The 5-hour or weekly quota counts as used up once its used percentage reaches this value; e.g. `95` leaves a little headroom. |

## Optional setting: how sessions appear in the Codex desktop app

Edit `%LOCALAPPDATA%\agent-fleet\config.json`; it takes effect after restarting the dispatcher. Re-running `install-worker.ps1` rewrites this file (the old one is kept as `config.json.bak`), so you need to add these again.

| Key | Default | Purpose |
|---|---|---|
| `resident_title_suffix` | `" - resident"` (`" - 固定"` in Chinese installs) | Appended to the session title of agents registered as resident, to tell them apart from one-off tasks; not added for the main agent. Only changes the title shown in the app; dispatching still uses the original name. Set to `""` to turn it off. |
| `codex_project_id` | not set | Put new sessions into this Codex project (through app-server's experimental `projectId` field). When not set, nothing changes. |
| `codex_app_folders` | on at install | Automatically files the dispatcher's sessions into two folders in the Codex desktop app sidebar: `main` (default `AgentFleet`) holds the main agent itself and every session it opens; `other` (default `AgentFleet-Other` in English installs, `AgentFleet-其他` in Chinese ones) holds sessions opened directly by the orchestrator or other agents. Both resident and one-off sessions go in; resident ones carry `resident_title_suffix` in their title. Write `{"main": "name", "other": "name"}` to rename, `true` to use the default names, `false` to turn it off. Codex engine only. |

The current desktop app (26.1007) does not yet show app-server projects. Sidebar folders are stored in the app's **internal file** `~/.codex/.codex-global-state.json`, and `codex_app_folders` works by editing that file:

- The app overwrites this file while it runs, so sessions are **only filed while the app is closed**: the dispatcher checks once after a task ends (then every 30 seconds), and if the app is open it waits until the app exits. New sessions appear in the folder the next time you open the app.
- The two folders are recognized by their root directory (`main` is the workspace, `other` is `tasks_dir`), so they are still recognized if you rename them in the app. Sessions you drag into another folder yourself are not moved back.
- Before each write the file is backed up as `.codex-global-state.json.bak-agent-fleet`. The file format is undocumented and may stop working after an app update; if it does, sessions simply aren't filed and tasks run as usual. `codex_app_folders` in `get_profile` shows the most recent result.

If you don't want the service to edit it automatically, you can turn off `codex_app_folders` and run the helper script by hand to put all sessions into one folder:

```powershell
# First fully quit the Codex desktop app (ChatGPT.exe); the script refuses to run if it detects the app running
python worker\tools\codex_app_folder.py --config "$env:LOCALAPPDATA\agent-fleet\config.json" --dry-run
python worker\tools\codex_app_folder.py --config "$env:LOCALAPPDATA\agent-fleet\config.json" [--folder-name AgentFleet] [--root <folder root path>]
```

## Development

```powershell
cd worker
<python> -m unittest test_dispatcher.py
claude plugin validate ..
```

See `AGENTS.md` for more conventions.

## License

MIT
