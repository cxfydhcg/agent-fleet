---
name: pair
description: Pair computers in the Agent Fleet with a one-time code, without copying connection.json. On a worker it opens a 10-minute pairing window and shows the code; on an orchestrator it searches the local network and Tailscale for workers waiting to pair and connects with the code the user gives. Use when the user invokes /pair, or asks to pair, connect or add a computer, get a new pairing code, or says a pairing code expired — e.g. "生成配对码", "搜索并配对新电脑，配对码 2726-9703", "pair with 192.168.1.23", "connect my other PC".
---

# Pair computers

Talk to the user in the language they write in. Resolve the plugin root from this skill's directory
(`../..`), or use `${CLAUDE_PLUGIN_ROOT}` in Claude Code. Commands below use `<plugin-root>`.

## 1. Which side is this computer on?

- **Worker capability** exists when a worker config exists: `%USERPROFILE%\AppData\Local\agent-fleet\config.json` on
  Windows, `~/Library/Application Support/agent-fleet/config.json` on macOS (or the user's custom data directory).
- **Orchestrator capability** requires completed orchestrator/both setup: the selected registry's
  `computers.json` must have the `orchestrator_session` field set by setup. The registry is
  `AGENT_FLEET_HOME`, or `~/.agent-fleet` by default. Never print its token files.
- A pure worker may only generate its own pairing code. If asked to search for or connect another
  computer, explain that orchestrator or both setup is required. A supplied code does not establish
  the role and is not permission to change it. With neither capability, use setup first.
- If both capabilities exist and the request doesn't specify the direction, ask whether to show a
  code for this computer or connect another computer. Outgoing discovery/pairing must run from the
  orchestrator session. If it is rejected, tell the user; run `claim-orchestrator` only if they ask to make
  this session the orchestrator.

## 2. Worker: show a pairing code

Only when the user asked for it: an open window lets whoever has the code take this computer's token.

```
"<python_executable from config.json>" "<plugin-root>/runtime/worker/tools/pairing.py" --config "<config.json>"
```

Relay the code, how long it is valid (10 minutes, works once, 5 wrong tries close it) and this computer's
address, then tell the user to say on the other computer: "search for new computers and pair, code <code>"
(or run this skill there). If the output says the worker is local only, other computers cannot reach it:
offer to rerun the setup skill with LAN or Tailscale connectivity. If the service is not answering, start it
(`service/worker/start.sh` on macOS, `service\worker\start.ps1` on Windows, in the workspace) and retry.

## 3. Orchestrator: search, then pair

The client is `node "<plugin-root>/scripts/fleet.mjs"`. In Codex, network access may need the user's approval.
It rejects discovery and outgoing pairing without management setup or outside its configured session,
before making network requests or saving connection data. Remote connection-file imports also require
management setup; local self-registration during worker setup remains allowed.

1. `discover` lists workers currently waiting to pair (local networks and online Tailscale devices, about
   two seconds). Show what was found: computer name, management name, address, and whether it is already
   registered.
2. Get the code the worker shows from the user. Never guess, reuse or invent a code.
3. Pair:
   - exactly one found: `pair <code>`
   - several found: ask which one, then `pair <computer name or IP> <code>`
   - none found: ask for the worker's IP, then `pair <IP> <code>`; if that fails, relay the hint
     (code expired or used, firewall, different network).
4. Report the result: computer, management name, main agent id. If it has no management name, offer
   `set-manager-name <computer> <name>`. Optionally verify with
   `export <computer> "Reply with exactly OK. Do not use tools or dispatch child tasks."`.

Pairing a computer that is already registered updates its entry in place (same main agent id).

## Safety

- The connection info includes the worker's token. Never print the token or the contents of `connection.json`.
- A pairing code is a secret for ten minutes: only use one the user gave you in this conversation.
- Copying the worker's `connection.json` and running `add-computer <path>` remains the fallback.
