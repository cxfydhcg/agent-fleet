// orchestrator agent（Claude）用的命令行客户端：通过各电脑的派任务 MCP 服务，和电脑上的 Agent 对话。
// 默认找每台电脑的“主agent export”；特殊情况下也可以直接找那台电脑上的其他 Agent（某个已命名的会话）。
//
// 用法:
//   node fleet.mjs overview                           所有电脑的档案总览：硬件、用途、软件、账号、Agent 及其职责（离线的显示最后一次的档案）
//   node fleet.mjs profile  <电脑> [--refresh]         一台电脑的完整档案；--refresh 重新检测硬件（平时不用）
//   node fleet.mjs describe-computer <电脑> [--role=用途] [--notes=备注] [--software=软件1,软件2]
//   node fleet.mjs describe-agent <电脑> <Agent名> --role=职责 [--tags=gpu,网页] [--type=main|resident]
//   node fleet.mjs remove-agent <电脑> <Agent名>       从档案里去掉（会话和历史保留）
//   node fleet.mjs watch                              持续盯着所有电脑的新通知，每条输出一行（给后台监视用；从上次看到的位置继续）
//   node fleet.mjs notifications <电脑> [--after=编号] 读通知全文（默认最近 10 条）
//   node fleet.mjs add-computer <connection.json>      接入一台新电脑（文件由那台电脑的 install-worker.ps1 生成）
//   node fleet.mjs computers                          列出已登记的电脑和 Agent
//   node fleet.mjs agents   <电脑>                    列出这台电脑上能对话的 Agent（已登记的 + 派任务服务里有名字的会话）
//   node fleet.mjs agent    <电脑> <Agent名> <消息> [--new] [--workspace=绝对路径]
//                                                     和指定 Agent 对话：续接它的会话，等待并输出回复。
//                                                     --new：找不到这个名字时，新建一个这样命名的 Agent。
//   node fleet.mjs export   <电脑> <消息>              等同于 agent <电脑> "主agent export" <消息>
//   node fleet.mjs submit   <电脑> <prompt> [--name=会话名] [--wait]   直接开新任务（特殊情况用）
//   node fleet.mjs continue <电脑> <task_id> <prompt>
//   node fleet.mjs get      <电脑> <task_id>
//   node fleet.mjs wait     <电脑> <task_id>
//   node fleet.mjs list     <电脑> [数量]
//   node fleet.mjs tools    <电脑>
// 每台电脑用登记表里的 url（没有就用 local_url）；加 --remote 强制走 Tailscale 地址。
// 登记表和各电脑令牌在 ~/.agent-fleet/（或环境变量 AGENT_FLEET_HOME），不在项目文件夹里。
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const MAIN_AGENT = "主agent export";
const fleetHome = process.env.AGENT_FLEET_HOME || join(homedir(), ".agent-fleet");
const registryPath = join(fleetHome, "computers.json");
mkdirSync(join(fleetHome, "tokens"), { recursive: true });
const registry = existsSync(registryPath) ? JSON.parse(readFileSync(registryPath, "utf8")) : { computers: {} };
// Older registries kept a single main_agent_export entry per computer.
for (const pc of Object.values(registry.computers)) {
  pc.agents ??= {};
  if (pc.main_agent_export) {
    const { name, ...rest } = pc.main_agent_export;
    pc.agents[name || MAIN_AGENT] ??= rest;
    delete pc.main_agent_export;
  }
}
const saveRegistry = () => writeFileSync(registryPath, JSON.stringify(registry, null, 2) + "\n");

const argv = process.argv.slice(2);
const flag = (name) => argv.includes(name);
const option = (name) => argv.find((a) => a.startsWith(`--${name}=`))?.slice(name.length + 3);
const [cmd, computer, ...rest] = argv.filter((a) => !a.startsWith("--"));
const out = (o) => console.log(JSON.stringify(o, null, 2));

const TERMINAL = new Set(["completed", "failed", "cancelled", "interrupted", "timed_out"]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let nextId = 1;
let url, token;

// This computer's own worker is reached locally; others over Tailscale.
const endpoint = (target) => (flag("--remote") ? target.tailscale_url : target.url || target.local_url || target.tailscale_url);

function connect(target) {
  url = endpoint(target);
  token = readFileSync(target.token_file, "utf8").trim();
  return rpc("initialize", { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "fleet-master", version: "1.2" } });
}

// A readable summary of one computer's profile, used by overview.
function summarize(name, profile, online) {
  const m = profile?.machine ?? {}, c = profile?.computer ?? {};
  const gpu = m.nvidia?.length ? m.nvidia.map((g) => `${g.name} ${Math.round(g.vram_total_mb / 1024)}GB`).join(", ")
    : (m.gpus ?? []).map((g) => g.name).filter((n) => !/virtual|remote|basic/i.test(n)).join(", ") || "无独立显卡";
  return {
    computer: name, online, ...(online ? {} : { last_seen: profile?._fetched_at ?? null }),
    hardware: [m.cpu, m.ram_gb && `${m.ram_gb}GB 内存`, gpu, m.os].filter(Boolean).join(" · ") || null,
    network: [m.egress_country && `出口 ${m.egress_country}`, m.tailscale?.offers_exit_node && "提供出口节点"].filter(Boolean).join(" · ") || null,
    role: c.role ?? null, notes: c.notes ?? null, software: c.software ?? [], accounts: c.accounts ?? [],
    busy: profile?.busy ?? null,
    agents: (profile?.agents ?? []).map((a) => ({ name: a.name, type: a.type, role: a.role, tags: a.tags, status: a.status })),
  };
}

async function fetchNotifications(target, after) {
  const base = endpoint(target).replace(/\/mcp\/?$/, "");
  const res = await fetch(`${base}/api/notifications?after=${after}`, {
    headers: { Authorization: `Bearer ${readFileSync(target.token_file, "utf8").trim()}` },
    signal: AbortSignal.timeout(10000),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

if (cmd === "watch") {
  // First run starts from "now" so old notifications are not replayed.
  for (const target of Object.values(registry.computers)) {
    if (target.last_notification_id == null) {
      try { target.last_notification_id = (await fetchNotifications(target, 0)).latest_id; } catch { /* offline: start at 0 later */ }
    }
  }
  saveRegistry();
  for (;;) {
    for (const [name, target] of Object.entries(registry.computers)) {
      try {
        const { notifications } = await fetchNotifications(target, target.last_notification_id ?? 0);
        for (const n of notifications) {
          console.log(`[${name}] #${n.id} ${n.needs_human ? "【需要人处理】" : ""}${n.sender || "agent"}：${n.title}`);
          target.last_notification_id = n.id;
        }
        if (notifications.length) saveRegistry();
      } catch { /* computer offline or restarting: try again next round */ }
    }
    await sleep(15000);
  }
}
if (cmd === "notifications") {
  const target = registry.computers[computer];
  if (!target) { console.error(`未知电脑: ${computer}`); process.exit(2); }
  const data = await fetchNotifications(target, Number(option("after") ?? 0));
  out(option("after") ? data.notifications : data.notifications.slice(-10));
  process.exit(0);
}
if (cmd === "add-computer") {
  // connection.json comes from install-worker.ps1 on the new computer: { name, tailscale_url, local_url, token, description }
  const info = JSON.parse(readFileSync(computer, "utf8").replace(/^﻿/, ""));
  if (!info.name || !info.token || !(info.tailscale_url || info.local_url)) {
    console.error("connection.json 缺少 name、token 或地址");
    process.exit(2);
  }
  const tokenFile = join(fleetHome, "tokens", `${info.name}.txt`);
  writeFileSync(tokenFile, info.token.trim() + "\n");
  const existing = registry.computers[info.name] ?? {};
  const isThisComputer = option("local") === "1";
  registry.computers[info.name] = {
    ...existing, description: info.description ?? existing.description ?? "",
    url: isThisComputer ? info.local_url : info.tailscale_url,
    local_url: info.local_url, tailscale_url: info.tailscale_url, token_file: tokenFile,
    agents: existing.agents ?? { [MAIN_AGENT]: { workspace: info.main_agent_workspace } },
  };
  saveRegistry();
  console.error(`已接入 ${info.name}（${registry.computers[info.name].url}）。可以删除这份 connection.json 了，令牌已保存在 ${tokenFile}。`);
  process.exit(0);
}
if (cmd === "computers") {
  out(Object.fromEntries(Object.entries(registry.computers).map(([n, pc]) => [n, { description: pc.description, agents: Object.keys(pc.agents) }])));
  process.exit(0);
}
if (cmd === "overview") {
  const rows = [];
  for (const [name, target] of Object.entries(registry.computers)) {
    try {
      await connect(target);
      const profile = await call("get_profile", {});
      target.last_profile = { ...profile, _fetched_at: new Date().toISOString() };
      rows.push(summarize(name, profile, true));
    } catch (error) {
      rows.push({ ...summarize(name, target.last_profile, false), error: String(error.message).slice(0, 200) });
    }
  }
  saveRegistry();
  out(rows);
  process.exit(0);
}
const pc = registry.computers[computer];
if (!pc) {
  console.error(`未知电脑: ${computer}。已登记: ${Object.keys(registry.computers).join(", ")}`);
  process.exit(2);
}

async function rpc(method, params) {
  const body = { jsonrpc: "2.0", id: nextId++, method, params };
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
    },
    body: JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`${method} HTTP ${res.status}: ${text.slice(0, 500)}`);
  const msg = JSON.parse(text);
  if (msg.error) throw new Error(`${method}: ${JSON.stringify(msg.error)}`);
  return msg.result;
}

async function call(tool, args) {
  const r = await rpc("tools/call", { name: tool, arguments: args });
  const t = r.content?.find((c) => c.type === "text")?.text;
  const payload = r.structuredContent?.result ?? r.structuredContent ?? (() => { try { return JSON.parse(t); } catch { return { text: t }; } })();
  if (r.isError) throw new Error(`${tool}: ${t}`);
  return payload;
}

async function wait(taskId) {
  for (let i = 0; ; i++) {
    const t = await call("get_task", { task_id: taskId });
    if (TERMINAL.has(t.status)) return t;
    if (i % 6 === 0) console.error(`  ...${t.status} (${i * 5}s)`);
    await sleep(5000);
  }
}

// Named sessions the dispatcher knows about: newest finished task per name.
async function namedSessions() {
  const { tasks } = await call("list_tasks", { limit: 100 });
  const byName = new Map();
  for (const t of tasks) {
    if (!t.name || !t.thread_id || t.archived) continue; // archived in the Codex app = removed by the user
    const entry = byName.get(t.name) ?? { latest: null, sessions: new Set(), status: t.status };
    entry.sessions.add(t.thread_id);
    if (!entry.latest && TERMINAL.has(t.status)) entry.latest = t;
    byName.set(t.name, entry);
  }
  return byName;
}

// The agent may be busy, e.g. handling an automatic wake-up after its sub-task finished: wait for its turn.
async function continueWhenFree(taskId, prompt) {
  for (let i = 0; i < 360; i++) {
    try {
      return await call("continue_task", { task_id: taskId, prompt });
    } catch (error) {
      if (!/pending work/.test(error.message)) throw error;
      if (i === 0) console.error("  对方正在处理别的事（例如子任务完成后的自动通知），排队等它空闲...");
      await sleep(5000);
    }
  }
  throw new Error("等了 30 分钟对方仍在忙");
}

async function talk(agentName, prompt) {
  let agent = pc.agents[agentName];
  let started;
  if (agent?.last_task_id) {
    started = await continueWhenFree(agent.last_task_id, prompt);
  } else {
    const found = agent ? null : (await namedSessions()).get(agentName);
    if (found?.latest) {
      if (found.sessions.size > 1) console.error(`注意：有 ${found.sessions.size} 个叫「${agentName}」的会话，使用最近的一个。`);
      started = await continueWhenFree(found.latest.task_id, prompt);
      agent = pc.agents[agentName] = {};
    } else if (agent || flag("--new")) {
      const workspace = option("workspace") ?? agent?.workspace;
      started = await call("submit_task", { prompt, name: agentName, ...(workspace ? { workspace } : {}) });
      agent = pc.agents[agentName] = { ...(agent ?? {}), ...(workspace ? { workspace } : {}) };
    } else {
      const names = [...new Set([...Object.keys(pc.agents), ...(await namedSessions()).keys()])];
      console.error(`电脑 ${computer} 上没有叫「${agentName}」的 Agent。可用: ${names.join("、") || "（无）"}。要新建请加 --new。`);
      process.exit(3);
    }
  }
  console.error(`已发给 ${computer} 的「${agentName}」（任务 ${started.task_id}），等待回复...`);
  const done = await wait(started.task_id);
  if (done.status === "completed") {
    Object.assign(agent, { last_task_id: done.task_id, thread_id: done.thread_id });
    saveRegistry();
  }
  const locked = /held by another Codex window|桌面 App 里打开过/.test(done.error || "");
  out({ computer, agent: agentName, task_id: done.task_id, thread_id: done.thread_id, status: done.status,
        reply: done.result, error: done.error,
        ...(locked ? { hint: "这个会话在 Codex App 里被打开过，被 App 锁住了。彻底退出 Codex App 后再试，或让主agent export 另开一个任务。" } : {}) });
}

await connect(pc);
const list = (value) => value?.split(/[,，]/).map((s) => s.trim()).filter(Boolean);

switch (cmd) {
  case "agents": {
    const sessions = await namedSessions();
    const described = Object.fromEntries((await call("get_profile", {})).agents.map((a) => [a.name, a]));
    const names = [...new Set([...Object.keys(described), ...Object.keys(pc.agents), ...sessions.keys()])];
    out(names.map((n) => ({ agent: n, default: n === MAIN_AGENT, role: described[n]?.role ?? null, tags: described[n]?.tags ?? [],
                            long_lived: n in described, status: sessions.get(n)?.status ?? null, sessions: sessions.get(n)?.sessions.size ?? 0 })));
    saveRegistry();
    break;
  }
  case "profile": {
    const profile = await call("get_profile", { refresh: flag("--refresh") });
    pc.last_profile = { ...profile, _fetched_at: new Date().toISOString() };
    saveRegistry();
    out(profile);
    break;
  }
  case "describe-computer":
    out(await call("update_computer_profile", { role: option("role"), notes: option("notes"), software: list(option("software")) }));
    break;
  case "describe-agent":
    out(await call("register_agent", { name: rest[0], role: option("role"), tags: list(option("tags")), agent_type: option("type") ?? (rest[0] === MAIN_AGENT ? "main" : "resident") }));
    break;
  case "remove-agent":
    out(await call("remove_agent", { name: rest[0] }));
    break;
  case "agent":
    await talk(rest[0], rest.slice(1).join(" "));
    break;
  case "export":
    await talk(MAIN_AGENT, rest.join(" "));
    break;
  case "tools":
    out((await rpc("tools/list", {})).tools.map((t) => t.name));
    break;
  case "submit": {
    const name = option("name");
    const started = await call("submit_task", { prompt: rest.join(" "), ...(name ? { name } : {}) });
    out(flag("--wait") ? await wait(started.task_id) : started);
    break;
  }
  case "continue":
    out(await call("continue_task", { task_id: rest[0], prompt: rest.slice(1).join(" ") }));
    break;
  case "get":
    out(await call("get_task", { task_id: rest[0] }));
    break;
  case "wait":
    out(await wait(rest[0]));
    break;
  case "list":
    out(await call("list_tasks", { limit: Number(rest[0] ?? 20) }));
    break;
  default:
    console.error("未知命令，见文件开头的用法说明");
    process.exit(2);
}
