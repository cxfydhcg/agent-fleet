---
name: update
description: Update Agent Fleet to the latest release - this computer's plugin (Claude Code and Codex) and worker, and, from the orchestrator session, every registered computer. Use for /update, "更新插件", "更新 agent fleet", "全部更新", "update all workers".
---

# Update Agent Fleet

Reply in the user's language. Never print tokens or connection files.

## 1. This computer

1. Update the plugin in every app installed here (skip an app whose command is missing):
   - Codex: `codex plugin marketplace upgrade agent-fleet`, then `codex plugin add agent-fleet@agent-fleet`
   - Claude Code: `claude plugin marketplace update agent-fleet`, then `claude plugin update agent-fleet@agent-fleet`
2. The new version lands in the plugin cache (`~/.claude/plugins/cache/agent-fleet/agent-fleet/<version>` or
   `~/.codex/plugins/cache/agent-fleet/agent-fleet/<version>`); use the highest version as `<new-root>`. This
   session's own plugin root may still point at the old version until the app restarts.
3. If this computer has a worker (`%USERPROFILE%\AppData\Local\agent-fleet\config.json` on Windows,
   `~/Library/Application Support/agent-fleet/config.json` on macOS, or a custom data directory), rerun its setup
   from the new version with its current permission mode: check `full_access` with
   `node "<new-root>/scripts/setup.mjs" --role worker --plan`, then run the same command without `--plan` and with
   `--full-access` or `--restricted`. Never add `--reconfigure`. Setup redeploys the newer code with the existing
   settings; when the service is busy it restarts itself once idle (`restart: when-idle`).

## 2. Every other computer (orchestrator session only)

If this session is the registered orchestrator, update each other registered computer with
`node "<new-root>/scripts/fleet.mjs" update <computer>` (list them with `computers`; skip this computer's own
worker, already done above). Each runs one short task on that computer, waits for its service to restart and
reports `version_before`, `version_after` and `restarted`. Run them in the background and in parallel; each can take
a few minutes. A computer that is offline, or not in full access (exit code 5), has to be updated by its owner
there with this skill.

## 3. Report

One line per computer: before -> after version, whether the service restarted, and any error. Mention that
the apps' "Restart to apply changes" only concerns their skill texts: new sessions pick up the new version,
and the worker services already run the new code.
