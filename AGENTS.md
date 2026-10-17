# Instructions for AI agents

This repository is **agent-fleet**: it lets one AI agent (the *orchestrator*, usually Claude Code) manage Codex agents on many computers. Read `README.md` first for the architecture. This file tells you how to **set it up for a user** and how to **work on the code**.

## Setting it up for a user

Ask the user which role this computer should have; it can have both:

- **Worker**: a computer whose agents do the work, such as video editing, browser tasks or desktop apps.
- **Orchestrator**: the computer where the user talks to Claude Code and manages the fleet.

### Worker (Windows)

1. Confirm the user wants this computer controlled by agents. Explain **`-FullAccess`**: sub-agents run without a sandbox and every permission prompt is auto-approved. They can then operate the desktop, browsers and apps, but also do anything else on the machine. Use it only if the user agrees. Without it, sub-agents can only write their own working folder.
2. From the repo root, run: `powershell -NoProfile -ExecutionPolicy Bypass -File .\install-worker.ps1 [-FullAccess] -Description "<what this computer is for>"`
   It installs missing Node.js / Python / Tailscale (via winget) and the Codex CLI. It also deploys the dispatcher, creates the computer's main agent ("主agent export") and adds autostart at logon.
3. Two steps need the human, and you cannot do them: **signing in to Tailscale** (the tray app, or `tailscale up`) and **signing in to Codex** (`codex login`). If the script warns about either, ask the user to do it, then re-run the script.
   The agents' working area is `%USERPROFILE%\AgentFleet\` (`main-agent\`, `tasks\`, `shared\`); pass `-Workspace <path>` to put it elsewhere, e.g. on a bigger disk for video work.
4. The script writes `%LOCALAPPDATA%\agent-fleet\connection.json`. It contains this computer's **token**: never print it, paste it into chat, or commit it. Tell the user to copy the file to their orchestrator computer.

### Orchestrator

1. Preferred: in Claude Code, `/plugin marketplace add cxfydhcg/agent-fleet`, then install the `agent-fleet` plugin. The skill is `/agent-fleet:call-agent`.
   Without the plugin: `powershell -NoProfile -ExecutionPolicy Bypass -File .\install-orchestrator.ps1` installs it as `/call-agent`. Add `-Codex` to also install it for Codex.
2. For each worker: `node <skill folder>/fleet.mjs add-computer <path to its connection.json>`. If the orchestrator is also a worker, add `--local=1`. Afterwards the user can delete the `connection.json` file.
3. Verify with `node <skill folder>/fleet.mjs overview`. Then send a harmless message, e.g. `/call-agent 【<computer>】 reply with "ok", don't use tools`.

### Rules while helping

- Never print, log or commit tokens (`token.txt`, `connection.json`, `.codex/config.toml` in the main agent workspace, `~/.agent-fleet/tokens/`).
- Don't open sessions that agents are using in the Codex desktop app. The app locks every session it has opened until Codex fully exits. Use the dashboard (`/dashboard` on port 8766 over Tailscale, or 8765 locally) to watch.
- Ask before deleting data, publishing anything, or changing account settings on the user's behalf.

## Working on the code

- The unified plugin is in `plugin/`; read `PLUGINS.md`. Its setup skill lets the user choose orchestrator, worker or both, and it packages skills for Codex and Claude Code. Run `python tools/build_plugins.py` after shared runtime/client edits to rebuild the bundled worker runtime and fleet client. Older generated role directories are not published in the catalogs or exports.
- `worker/claude_executor.py` adapts Claude Code print-mode sessions. It uses per-turn MCP task headers for callback attribution, preserves session IDs, and defaults to restricted file tools. Claude's tool permissions are not a Windows OS sandbox. Never add permission bypass flags unless the owner chose full access.
- Run the dispatcher and Claude adapter tests plus `tests/test_plugins.py`. Claude fixtures never call a model. Validate each plugin and the marketplace with `claude plugin validate`; source distribution must exclude machine data and credentials.

- `worker/server.py`: the dispatcher (FastMCP over Streamable HTTP; runs Codex through `codex app-server`). Per-computer config is passed with `--config`; nothing machine-specific belongs in the repo.
- Quota guard: before each app-server turn the dispatcher calls `account/rateLimits/read`; if the 5-hour (`primary`) or weekly (`secondary`) window is at `quota_guard_threshold` percent (default 100), the job goes back to `queued` and the whole queue waits for the reset. Config keys `quota_guard` and `quota_guard_threshold` are described in `README.md`. Errors and unknown answers must never block.
- `worker/test_dispatcher.py`: run it from `worker/` with `python -m unittest test_dispatcher.py`. Keep it passing and add tests for new behavior. Tests use a fake app-server and never call a real model.
- `worker/tools/codex_app_folder.py`: optional, stdlib-only helper that files the dispatcher's sessions into a Codex desktop app sidebar folder by editing the app's internal `~/.codex/.codex-global-state.json` (undocumented; may break with app updates). Keep its JSON transform in `add_to_folder` pure so it stays testable. The dispatcher uses the same transform (`sort_into_folders`) when config `codex_app_folders` is on, filing sessions into a main-agent folder and an other-agents folder only while the desktop app is closed. Optional config keys `resident_title_suffix`, `codex_project_id` and `codex_app_folders` are described in `README.md`.
- `worker/templates/main-agent/AGENTS.md`: the main agent's instructions; `{{COMPUTER_NAME}}` and `{{WORKSPACE}}` are filled in at install time.
- `plugin/skills/call-agent/`: the orchestrator skill and its client `fleet.mjs` (Node 18+, no dependencies).
- PowerShell scripts must run on Windows PowerShell 5.1: no `&&`, `??` or ternaries. Save them as UTF-8 **with BOM**, because they contain Chinese text. Variable names are case-insensitive: a `$Config` parameter would clobber a `$config` variable.
- Validate the plugin with `claude plugin validate .` after changing `.claude-plugin/` or `plugin/`.
