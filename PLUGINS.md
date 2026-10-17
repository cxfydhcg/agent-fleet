# Agent Fleet plugin

Install **one plugin**, then run setup and choose this computer's role:

| Role | What setup does |
| --- | --- |
| Orchestrator | Initialize the fleet registry and connect computers you manage. |
| Worker | Install this computer's main agent, task service, dashboard and autostart. |
| Both | Set up the worker and orchestrator, and register this worker locally automatically. |

The same plugin works in Codex and Claude Code. For a worker, separately choose
whether its main agent and tasks run Codex or Claude Code. It includes five skills:
setup, start, pair, main-agent and call-agent. Setup is the installation entry point: it asks whether
this computer controls other agents, is controlled by another computer, or both,
and also handles registering workers, renames, reconfiguration and repairs.

## Install

Use `cxfydhcg/agent-fleet` as the marketplace source for the published plugin.
For development, use the actual absolute path to this folder wherever <repository> appears.

On Windows, the included helper installs the single plugin:

```powershell
.\Install-Plugins.ps1 -Client Codex
.\Install-Plugins.ps1 -Client ClaudeCode
```

Each selected client must already be installed. Open a new chat after installing.

In Claude Code:

```text
/plugin marketplace add <repository>
/plugin install agent-fleet@agent-fleet
/agent-fleet:setup
```

To try without registering a marketplace, run
claude --plugin-dir <repository>/plugin.

In Codex CLI:

```text
codex plugin marketplace add <repository>
codex plugin add agent-fleet@agent-fleet
```

In a new Codex chat, ask "Use Agent Fleet setup." Choose orchestrator, worker,
or both when prompted. If your client shows a setup/onboarding button, that button
uses the same setup skill. Installation itself does not choose a role.

For the published version, replace the local marketplace path with cxfydhcg/agent-fleet.
The plugin keeps the existing agent-fleet identity and call-agent command.

## Run role setup directly

For an installed worker, use `$agent-fleet:start` in Codex or `/agent-fleet:start` in Claude Code
to start the service without reinstalling or changing configuration. The direct command is:

```text
node <repository>/plugin/scripts/start.mjs
```

It checks authenticated health, reuses an already-running service, and prints the local dashboard URL.
Use `--config PATH` (or `AGENT_FLEET_CONFIG`) for a custom data directory. No token appears in output.

Setup also adds the workspace to the Windows Codex desktop sidebar as a local
project, and verifies its saved registration. It uses the app's workspace URL;
it does not edit the running app's internal state file or open a live worker
session. This also runs when setup reuses an existing worker.
In Codex desktop, the setup skill also reads and pins the verified main-agent
chat when app tools are available, so it is visible under its configured name.
It preserves the same session and never opens it. Claude worker chats remain in
the worker dashboard. A sidebar project and a visible main-agent chat are checked
separately.

For orchestrator-only setup, the public project folder defaults to
`Documents/AgentFleet`; `--workspace PATH` chooses a different folder. Private
registries and tokens stay separate. If the desktop app is absent, setup reports
that explicitly. A `desktop.status` of `pending` means registration has not
completed; respond to any folder-access prompt in Codex, then rerun setup.
Use `--no-desktop` (PowerShell worker setup: `-NoDesktop`) for headless or
Claude Code-only operation. CLI-only setup doesn't claim a desktop project.

Node.js 18+ is required. Preview by adding --plan; it prints non-secret settings
without installing or creating a registry.

```text
node <repository>/plugin/scripts/setup.mjs --role orchestrator
node <repository>/plugin/scripts/setup.mjs --role worker --engine codex --full-access
node <repository>/plugin/scripts/setup.mjs --role both --engine claude --restricted
```

Both automatically connects this computer's worker using its local address.
No connection-file transfer is needed for that step. Remote workers still use
private connection-file transfer. --registry-home selects the orchestrator
registry, and --data-dir selects private worker data; these remain separate.

Worker settings include --engine codex|claude, --connectivity local|tailscale|lan,
--lan-address, --workspace, --name, --description, --port, --tailscale-port, --claude-executable,
--skip-prerequisites and --no-autostart. Worker setup needs --full-access or --restricted; ask the owner, since --full-access requires explicit agreement.
Use --reconfigure for an intentional worker repair/change. Role-specific setup
scripts remain available for advanced use.

Choosing orchestrator does not uninstall or stop a worker already installed on
the computer. Removing that worker is a separate, explicit action.

## Worker setup

Choose a main-agent name during setup, independently of the computer name:

```text
node <repository>/plugin/scripts/setup.mjs --role worker --main-agent-name "Main Agent Work" --restricted
```

To rename an existing main agent, keep its other settings (including its current `--full-access` or `--restricted`) and add `--reconfigure`:

```text
node <repository>/plugin/scripts/setup.mjs --role worker --main-agent-name "main agent windows pc 1" --full-access --reconfigure
```

The name supports spaces and Unicode (1 to 100 printable characters). Renaming
keeps the existing session and workspace. The `export` command always chooses
that computer's configured main agent. Setup preserves the chosen name on reruns.
If a remote orchestrator already registered the worker, import its updated private
connection file there to update the name and preserve its session pointer.
The `--name` option is the computer's identifier; `--main-agent-name` names its agent.

Worker installation currently supports Windows. It defaults to local-only
connectivity, restricted permissions, and a workspace in `Documents/AgentFleet`.
Choose the actual worker engine separately from the app used to invoke setup:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "<repository>\plugin\scripts\setup-worker.ps1" -Engine codex -Connectivity Local

powershell -NoProfile -ExecutionPolicy Bypass -File "<repository>\plugin\scripts\setup-worker.ps1" -Engine claude -Connectivity Local
```

The selected CLI must be signed in by the owner (`codex login` or
`claude auth login`). Claude Code requires 2.1.248 or newer. For remote workers,
choose `-Connectivity Tailscale`, then sign in to the same Tailscale network.
On a trusted local network you can choose `-Connectivity Lan` instead (setup.mjs:
`--connectivity lan`). The worker then listens on its local IPv4 address (auto-detected,
or `-LanAddress`) and `connection.json` carries a `lan_url`. LAN traffic is plain
HTTP, so the token is readable by anyone capturing traffic on that network. The owner
must allow inbound TCP on the worker port for Private networks in Windows Firewall.
Sign-in is a human step; the plugin never exports credentials or creates API keys.

Codex uses its workspace-write sandbox. Claude uses restricted mode with file
tools and pre-approved edits in working directories; shell/code execution is
disabled. Claude's restriction is a CLI tool boundary, not an OS sandbox.
`-FullAccess` enables unrestricted execution and automatic permission approval;
the setup skill must explain this and obtain explicit agreement first.

`-Plan` prints non-secret planned settings. Existing installations are reused.
Use `-Reconfigure` for an intentional repair, engine switch or network change;
the script preserves extra configuration keys and refuses to interrupt busy
workers. Session history remains on disk, but sessions cannot continue under a
different engine. Custom main-agent instructions and worker rules are preserved.

The worker runtime is copied to `workspace/service`, rather than running from the
plugin cache. Private data stays in the reported data directory, normally
`%LOCALAPPDATA%/agent-fleet`. Filesystem virtualization in a packaged desktop host
can change that physical location; setup reports and uses the resolved path.

## Connect the orchestrator

Run the setup skill and choose orchestrator (or both). Node.js 18+ is required (the worker's Claude
native installation may have additional platform requirements). The registry
defaults to `~/.agent-fleet`, or the existing `AGENT_FLEET_HOME`.

Privately transfer a remote worker's `connection.json` and register it:

```text
node <repository>/plugin/scripts/setup-orchestrator.mjs --connection <private-connection-file>
```

For the worker on this same computer, append `--local`. A local-only worker is
reachable only from its own computer. The setup script validates connection
metadata, preserves existing workers and never prints the token value.

Tell the orchestrator a goal, such as "Ask editing-pc's main agent to organize
these files." It uses the bundled call-agent client. Each computer's main agent
decides how to delegate, yields after dispatching, and is woken automatically
when children finish. Only one task runs per computer at a time.

The local dashboard is normally `http://127.0.0.1:8765/dashboard`. Its login asks
for the token from the private worker data directory. Codex sessions must not be
opened in the desktop app while the dispatcher needs to continue them. Claude
sessions have dashboard transcripts and CLI session IDs, with no Codex deep links.
Codex's quota guard applies to the Codex app-server engine; Claude failures and
limits are reported by its CLI, without claiming Codex-style quota-reset support.

## Build and verify

After changing shared runtime or the legacy fleet client, run:

```text
python tools/build_plugins.py
python -m unittest discover -s worker -p "test_*.py"
python -m unittest discover -s tests -p "test_*.py"
node --test tests/test_service_start.mjs
node --test tests/test_pair_roles.mjs
claude plugin validate .
claude plugin validate plugin
```

Use a Python environment with `worker/requirements.txt` for dispatcher tests.
The Claude adapter tests use a fake executable and never call a model. A live
Claude model turn still needs the owner's Claude sign-in. Plugin setup should be
smoke-tested after signing in: send the main agent "Reply with exactly OK. Do not
use tools or dispatch child tasks" and inspect a completed result.

Package only these repository files. Exclude machine data, authentication,
connection files, tokens, logs and `.git` from distribution. Keep the MIT license.

Authoring references: [Codex plugin packaging](https://developers.openai.com/plugins/build/plugins),
[Claude Code plugins](https://code.claude.com/docs/en/plugins), and
[Claude CLI](https://code.claude.com/docs/en/cli-reference).
