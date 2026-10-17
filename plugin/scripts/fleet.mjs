// orchestrator agent（Claude）用的命令行客户端：通过各电脑的派任务 MCP 服务，和电脑上的 Agent 对话。
// 默认找每台电脑的“主agent export”；特殊情况下也可以直接找那台电脑上的其他 Agent（某个已命名的会话）。
//
// 用法:
//   node fleet.mjs overview                           所有电脑的档案总览：硬件、用途、软件、账号、Agent 及其职责（离线的显示最后一次的档案）
//   node fleet.mjs profile  <电脑> [--refresh]         一台电脑的完整档案；--refresh 重新检测硬件（平时不用）
//   node fleet.mjs describe-computer <电脑> [--role=用途] [--notes=备注] [--software=软件1,软件2]
//   node fleet.mjs describe-agent <电脑> <Agent名> --role=职责 [--tags=gpu,网页] [--type=main|resident]
//                                    [--can-dispatch=on|off] [--locked=on|off]
//                                                     --can-dispatch=off：这个 Agent 不能再开新会话（派新任务）
//                                                     --locked=on：只有 orchestrator 能续接、取消、打开它的会话，主 Agent 也不行
//                                                     不加就保持原样；只有 orchestrator 能改这两项
//   node fleet.mjs remove-agent <电脑> <Agent名>       从档案里去掉（会话和历史保留）
//   node fleet.mjs watch                              持续盯着所有电脑的新通知，每条输出一行（给后台监视用；从上次看到的位置继续，
//                                                     位置记在 ~/.agent-fleet/watch-state.json）
//   node fleet.mjs notifications <电脑> [--after=编号] 读通知全文（默认最近 10 条）
//   node fleet.mjs add-computer <connection.json>      接入一台新电脑（文件由那台电脑的 install-worker.ps1 生成）
//   node fleet.mjs computers                          列出已登记的电脑和 Agent
//   node fleet.mjs discover [--hosts=IP,IP] [--port=8765]
//                                                     在局域网和 Tailscale 设备里找正在等配对的 worker（worker 上先显示配对码）
//   node fleet.mjs pair [<IP或电脑名>] <配对码>        用 worker 显示的配对码接入它，不用拷 connection.json；
//                                                     只给配对码时先自动搜索，搜到正好一台就接入它
//   node fleet.mjs set-manager-name <电脑> <管理名>    给这台电脑的主 Agent 起管理名（比如 经理cris），之后 <电脑> 处可以直接写管理名；
//                                                     管理名在整个机群里不能重复；写 "" 清除
//   node fleet.mjs dashboard [--port=8790] [--no-open] 在这台电脑上打开机群看板：一页看所有电脑的任务和会话（只读）；
//                                                     令牌由这里代为带上，不用在网页里粘贴；--no-open 只输出链接
//   node fleet.mjs language <zh|en>                   输出的语言（默认跟系统；也可用环境变量 AGENT_FLEET_LANG）
// <电脑> 可以写电脑名、主 Agent 的管理名，或主 Agent 的 ID。主 Agent 按安装时生成的 ID 认，
// 改主 Agent 的名字或管理名都不会找错。
//   node fleet.mjs agents   <电脑>                    列出这台电脑上能对话的 Agent（已登记的 + 派任务服务里有名字的会话）
//   node fleet.mjs agent    <电脑> <Agent名> <消息> [--new] [--workspace=绝对路径]
//                                                     和指定 Agent 对话：续接它的会话，等待并输出回复。
//                                                     --new：找不到这个名字时，新建一个这样命名的 Agent。
//   node fleet.mjs export   <电脑> <消息>              Send to that computer's configured main agent.
//   node fleet.mjs submit   <电脑> <prompt> [--name=会话名] [--wait] [--model=模型] [--effort=档位]   直接开新任务（特殊情况用）
//   node fleet.mjs continue <电脑> <task_id> <prompt> [--model=模型] [--effort=档位]
//                                                     --model/--effort 平时不加；只在会话一直报 "Selected model is at capacity"
//                                                     或用户要求时用。本轮生效，之后的续接沿用。
//   node fleet.mjs get      <电脑> <task_id>
//   node fleet.mjs wait     <电脑> <task_id>
//   node fleet.mjs list     <电脑> [数量]
//   node fleet.mjs cancel   <电脑> <task_id>...       取消排队中或运行中的任务（已做的改动保留）
//   node fleet.mjs tools    <电脑>
// 每台电脑用登记表里的 url（没有就用 local_url）；加 --remote 强制走 Tailscale（或局域网）地址。
// 登记表和各电脑令牌在 ~/.agent-fleet/（或环境变量 AGENT_FLEET_HOME），不在项目文件夹里。
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir, networkInterfaces } from "node:os";
import { execFileSync } from "node:child_process";
import { join } from "node:path";

const MAIN_AGENT = "主agent export";
const validAgentName = (name) => typeof name === 'string' && name.trim().length >= 1 && name.trim().length <= 100 && !/[\u0000-\u001f\u007f]/.test(name) && !name.trim().startsWith('--') && !['__proto__', 'constructor', 'prototype'].includes(name.trim());
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
    pc.main_agent_name ??= name || MAIN_AGENT;
    delete pc.main_agent_export;
  }
}
const mainAgentName = (pc) => pc.main_agent_name || MAIN_AGENT;
const saveRegistry = () => writeFileSync(registryPath, JSON.stringify(registry, null, 2) + "\n");
const systemLanguage = () => { try { return Intl.DateTimeFormat().resolvedOptions().locale; } catch { return ""; } };
const LANG = (process.env.AGENT_FLEET_LANG || registry.language || (systemLanguage().startsWith("zh") ? "zh" : "en")) === "en" ? "en" : "zh";
const T = (zh, en) => (LANG === "en" ? en : zh);

const argv = process.argv.slice(2);
const flag = (name) => argv.includes(name);
const option = (name) => argv.find((a) => a.startsWith(`--${name}=`))?.slice(name.length + 3);
const [cmd, computerArg, ...rest] = argv.filter((a) => !a.startsWith("--"));
const out = (o) => console.log(JSON.stringify(o, null, 2));
// A computer is named by its registry name, its main agent's management name, or its main agent's id.
function resolveComputer(key) {
  if (key == null || Object.hasOwn(registry.computers, key)) return key;
  const wanted = key.trim().toLowerCase();
  return Object.keys(registry.computers).find((n) => registry.computers[n].manager_name?.toLowerCase() === wanted ||
                                                     registry.computers[n].main_agent_id === key.trim()) ?? key;
}
const computer = cmd === "add-computer" ? computerArg : resolveComputer(computerArg);
// Management names are how people and the orchestrator call a main agent, so they must not collide with
// another computer's name or management name.
function managerNameConflict(name, self) {
  const wanted = name.trim().toLowerCase();
  return Object.entries(registry.computers).find(([n, other]) => n !== self &&
    (n.toLowerCase() === wanted || other.manager_name?.toLowerCase() === wanted))?.[0];
}

const TERMINAL = new Set(["completed", "failed", "cancelled", "interrupted", "timed_out"]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let nextId = 1;
let url, token;

// This computer's own worker is reached locally; others over Tailscale or the LAN.
const endpoint = (target) => (flag("--remote") ? target.tailscale_url || target.lan_url : target.url || target.local_url || target.tailscale_url || target.lan_url);

function connect(target) {
  url = endpoint(target);
  token = readFileSync(target.token_file, "utf8").trim();
  return rpc("initialize", { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "fleet-master", version: "1.2" } });
}

// A readable summary of one computer's profile, used by overview.
function summarize(name, profile, online) {
  const m = profile?.machine ?? {}, c = profile?.computer ?? {};
  const gpu = m.nvidia?.length ? m.nvidia.map((g) => `${g.name} ${Math.round(g.vram_total_mb / 1024)}GB`).join(", ")
    : (m.gpus ?? []).map((g) => g.name).filter((n) => !/virtual|remote|basic/i.test(n)).join(", ") || T("无独立显卡", "no dedicated GPU");
  const pc = registry.computers[name] ?? {};
  return {
    computer: name, manager_name: pc.manager_name ?? null, main_agent_id: pc.main_agent_id ?? profile?.main_agent?.id ?? null,
    online, ...(online ? {} : { last_seen: profile?._fetched_at ?? null }),
    hardware: [m.cpu, m.ram_gb && T(`${m.ram_gb}GB 内存`, `${m.ram_gb}GB RAM`), gpu, m.os].filter(Boolean).join(" · ") || null,
    network: [m.egress_country && T(`出口 ${m.egress_country}`, `egress ${m.egress_country}`),
              m.tailscale?.offers_exit_node && T("提供出口节点", "offers an exit node")].filter(Boolean).join(" · ") || null,
    role: c.role ?? null, notes: c.notes ?? null, software: c.software ?? [], accounts: c.accounts ?? [],
    busy: profile?.busy ?? null,
    // Older workers don't report it; null means unknown, not restricted.
    permissions: profile?.permissions ? (profile.permissions.full_access ? T("完全访问（可操作桌面和应用）", "full access (can operate the desktop and apps)")
      : T(`受限（${profile.permissions.sandbox}，需要批准的操作如 Computer Use 会被拒绝）`, `restricted (${profile.permissions.sandbox}; anything needing approval, such as Computer Use, is refused)`)) : null,
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
  // The cursor has its own file and watch never writes computers.json: other commands (e.g. a long agent
  // call) load the registry early and save it late, which used to put an old position back and replay.
  const statePath = join(fleetHome, "watch-state.json");
  const seen = existsSync(statePath) ? JSON.parse(readFileSync(statePath, "utf8")) : {};
  const saveSeen = () => writeFileSync(statePath, JSON.stringify(seen, null, 2) + "\n");
  for (const [name, target] of Object.entries(registry.computers)) {
    seen[name] ??= target.last_notification_id; // older versions kept it in the registry
    // First run starts from "now" so old notifications are not replayed.
    if (seen[name] == null) {
      try { seen[name] = (await fetchNotifications(target, 0)).latest_id; } catch { /* offline: start at 0 later */ }
    }
  }
  saveSeen();
  for (;;) {
    for (const [name, target] of Object.entries(registry.computers)) {
      try {
        const { notifications } = await fetchNotifications(target, seen[name] ?? 0);
        for (const n of notifications) {
          console.log(`[${registry.computers[name].manager_name || name}] #${n.id} ${n.needs_human ? T("【需要人处理】", "[needs a person] ") : ""}${n.sender || "agent"}${T("：", ": ")}${n.title}`);
          seen[name] = n.id;
        }
        if (notifications.length) saveSeen();
      } catch { /* computer offline or restarting: try again next round */ }
    }
    await sleep(15000);
  }
}
if (cmd === "language") {
  if (!["zh", "en"].includes(computerArg)) { console.error("language <zh|en>"); process.exit(2); }
  registry.language = computerArg;
  saveRegistry();
  console.error(computerArg === "en" ? "Output language: English." : "输出语言：中文。");
  process.exit(0);
}
if (cmd === "notifications") {
  const target = registry.computers[computer];
  if (!target) { console.error(T(`未知电脑: ${computer}`, `Unknown computer: ${computer}`)); process.exit(2); }
  const data = await fetchNotifications(target, Number(option("after") ?? 0));
  out(option("after") ? data.notifications : data.notifications.slice(-10));
  process.exit(0);
}
// connection.json comes from install-worker.ps1 on the new computer, or from pairing:
// { name, tailscale_url, lan_url, local_url, token, description, main_agent_id, manager_name, ... }
function addComputer(info, isThisComputer, source) {
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(info.name || "")) {
    console.error("Computer name must use letters, numbers, dots, underscores or hyphens, beginning with a letter or number.");
    process.exit(2);
  }
  if (!info.name || !info.token || !(info.tailscale_url || info.lan_url || info.local_url)) {
    console.error(T("connection.json 缺少 name、token 或地址", "connection.json is missing the name, token or address"));
    process.exit(2);
  }
  if (info.main_agent_name !== undefined && !validAgentName(info.main_agent_name)) {
    console.error('Main agent name must contain 1 to 100 printable characters, without a leading --.');
    process.exit(2);
  }
  // The same main agent (same id) under a new computer name is that computer renamed, not a new one.
  const renamedFrom = info.main_agent_id && Object.keys(registry.computers).find((n) => n !== info.name &&
    registry.computers[n].main_agent_id === info.main_agent_id);
  const existing = registry.computers[info.name] ?? (renamedFrom ? registry.computers[renamedFrom] : {});
  const managerName = (info.manager_name?.trim() || existing.manager_name || "").trim();
  if (managerName && (!validAgentName(managerName) || managerNameConflict(managerName, renamedFrom || info.name))) {
    console.error(T(`管理名「${managerName}」无效，或已经被 ${managerNameConflict(managerName, renamedFrom || info.name)} 用了。`,
                    `The management name "${managerName}" is invalid or already used by ${managerNameConflict(managerName, renamedFrom || info.name)}.`));
    process.exit(2);
  }
  const oldMain = mainAgentName(existing);
  const chosenMain = info.main_agent_name?.trim() || oldMain;
  if (chosenMain !== oldMain && Object.hasOwn(existing.agents ?? {}, chosenMain)) {
    console.error('An agent already uses the requested main agent name; choose another name.');
    process.exit(2);
  }
  const tokenFile = join(fleetHome, "tokens", `${info.name}.txt`);
  writeFileSync(tokenFile, info.token.trim() + "\n");
  const engine = info.engine || 'codex';
  const agents = { ...(existing.agents ?? {}) };
  if (engine !== (existing.engine || 'codex')) {
    // History remains on the worker, but a different CLI cannot resume those sessions.
    for (const [name, value] of Object.entries(agents)) {
      const { last_task_id, thread_id, ...metadata } = value;
      agents[name] = metadata;
    }
  }
  if (chosenMain !== oldMain && Object.hasOwn(agents, oldMain)) {
    agents[chosenMain] = agents[oldMain];
    delete agents[oldMain];
  }
  agents[chosenMain] = { ...(agents[chosenMain] ?? {}), ...(info.main_agent_workspace ? { workspace: info.main_agent_workspace } : {}) };
  if (renamedFrom) delete registry.computers[renamedFrom];
  registry.computers[info.name] = {
    ...existing, engine, main_agent_name: chosenMain, description: info.description ?? existing.description ?? "",
    ...(info.main_agent_id ? { main_agent_id: info.main_agent_id } : {}), ...(managerName ? { manager_name: managerName } : {}),
    url: isThisComputer ? info.local_url : info.tailscale_url || info.lan_url,
    local_url: info.local_url, tailscale_url: info.tailscale_url, lan_url: info.lan_url, token_file: tokenFile,
    agents,
  };
  saveRegistry();
  if (renamedFrom) console.error(T(`${renamedFrom} 改名为 ${info.name}（同一个主 Agent），原来的设置都保留。`,
                                    `${renamedFrom} was renamed to ${info.name} (same main agent); its settings are kept.`));
  console.error(source === "pair"
    ? T(`已接入 ${info.name}（${registry.computers[info.name].url}），令牌已保存在 ${tokenFile}。`, `Connected ${info.name} (${registry.computers[info.name].url}); the token is saved in ${tokenFile}.`)
    : T(`已接入 ${info.name}（${registry.computers[info.name].url}）。可以删除这份 connection.json 了，令牌已保存在 ${tokenFile}。`,
        `Connected ${info.name} (${registry.computers[info.name].url}). You can delete that connection.json; the token is saved in ${tokenFile}.`));
}
if (cmd === "add-computer") {
  addComputer(JSON.parse(readFileSync(computer, "utf8").replace(/^\ufeff/, "")), option("local") === "1", "file");
  process.exit(0);
}

// Where workers waiting to pair might be: this computer's local networks (at most 1024 addresses each) on
// the worker port, and online Tailscale devices on the Tailscale port. --hosts limits the search.
const ipToInt = (ip) => ip.split(".").reduce((n, part) => n * 256 + Number(part), 0);
const intToIp = (n) => [24, 16, 8, 0].map((shift) => Math.floor(n / 2 ** shift) % 256).join(".");
function searchTargets() {
  const port = Number(option("port") ?? 8765), tailscalePort = Number(option("tailscale-port") ?? 8766);
  const given = option("hosts")?.split(/[,，\s]+/).filter(Boolean);
  if (given) return given.map((h) => (/:\d+$/.test(h) ? h : `${h}:${port}`));
  const targets = new Set();
  for (const address of Object.values(networkInterfaces()).flat()) {
    if (!address || address.internal || !(address.family === "IPv4" || address.family === 4)) continue;
    const ip = ipToInt(address.address), mask = ipToInt(address.netmask || "255.255.255.0");
    if (address.address.startsWith("169.254.") || (ip >= ipToInt("100.64.0.0") && ip <= ipToInt("100.127.255.255"))) continue;
    // The whole subnet when it is small enough, otherwise the /24 around this computer.
    const block = 2 ** 32 - mask, size = block <= 1024 ? block : 256;
    const start = Math.floor(ip / size) * size;
    for (let host = start + 1; host < start + size - 1; host++) if (host !== ip) targets.add(`${intToIp(host)}:${port}`);
  }
  for (const binary of ["tailscale", "C:\\Program Files\\Tailscale\\tailscale.exe", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]) {
    try {
      const status = JSON.parse(execFileSync(binary, ["status", "--json"], { timeout: 5000, stdio: ["ignore", "pipe", "ignore"] }));
      for (const peer of Object.values(status.Peer ?? {})) if (peer.Online && peer.TailscaleIPs?.[0]) targets.add(`${peer.TailscaleIPs[0]}:${tailscalePort}`);
      break;
    } catch { /* not installed or not signed in */ }
  }
  return [...targets];
}

async function discover() {
  const targets = searchTargets(), found = [];
  let next = 0;
  const probe = async () => {
    while (next < targets.length) {
      const target = targets[next++];
      try {
        const res = await fetch(`http://${target}/pair/info`, { signal: AbortSignal.timeout(1200) });
        const info = res.ok ? await res.json() : null;
        if (info?.service === "agent-fleet" && info.pairing) {
          const registered = Object.keys(registry.computers).find((n) => registry.computers[n].main_agent_id === info.main_agent_id);
          found.push({ address: target, ...info, ...(registered ? { already_registered_as: registered } : {}) });
        }
      } catch { /* nothing there, or not a worker */ }
    }
  };
  await Promise.all(Array.from({ length: 128 }, probe));
  return found;
}

if (cmd === "discover") {
  const found = await discover();
  if (!found.length) {
    console.error(T("没有搜到正在等配对的 worker。请确认：worker 上刚显示过配对码（10 分钟内有效，过期就在 worker 上运行 pair.ps1）；"
                    + "worker 用的是 LAN 或 Tailscale 方式；防火墙放行了端口；两台电脑在同一个网络。也可以直接用 pair <worker 的 IP> <配对码>。",
                    "No worker waiting to pair was found. Check that the worker just showed a pairing code (valid 10 minutes; run pair.ps1 "
                    + "on the worker for a new one), that it was set up with LAN or Tailscale, that its firewall allows the port, and that both "
                    + "computers are on the same network. You can also run pair <worker IP> <code>."));
    process.exit(3);
  }
  out(found.map(({ address, computer_name, manager_name, main_agent_name, role, engine, expires_in, already_registered_as }) =>
    ({ address, computer_name, manager_name, main_agent_name, role, engine, expires_in, already_registered_as })));
  process.exit(0);
}
if (cmd === "pair") {
  const code = rest[0] ?? computerArg, wanted = rest[0] ? computerArg : null;
  if (!/^\d{4}-?\d{4}$/.test(code ?? "")) {
    console.error(T("用法：pair [<IP或电脑名>] <配对码>，配对码是 worker 上显示的 8 位数字", "Usage: pair [<IP or computer name>] <code>; the code is the 8 digits the worker shows"));
    process.exit(2);
  }
  const asAddress = (host) => (/:\d+$/.test(host) ? host : `${host}:${option("port") ?? 8765}`);
  let address = wanted && /^\d+\.\d+\.\d+\.\d+(:\d+)?$/.test(wanted) ? asAddress(wanted) : null;
  if (!address) {
    const found = (await discover()).filter((f) => !wanted || f.computer_name === wanted || f.manager_name === wanted);
    if (!found.length && wanted?.includes(".")) found.push({ address: asAddress(wanted) }); // a host name such as worker.local
    if (found.length !== 1) {
      console.error(found.length
        ? T(`搜到 ${found.length} 台在等配对的电脑：${found.map((f) => `${f.computer_name}（${f.address}）`).join("、")}。请指定一台：pair <IP或电脑名> <配对码>`,
            `Found ${found.length} workers waiting to pair: ${found.map((f) => `${f.computer_name} (${f.address})`).join(", ")}. Pick one: pair <IP or computer name> <code>`)
        : T("没有搜到正在等配对的 worker。请直接给 IP：pair <worker 的 IP> <配对码>（先运行 discover 看看原因提示）。",
            "No worker waiting to pair was found. Give its address: pair <worker IP> <code> (run discover for hints)."));
      process.exit(3);
    }
    address = found[0].address;
  }
  let res;
  try {
    res = await fetch(`http://${address}/pair/claim`, { method: "POST", headers: { "Content-Type": "application/json" },
                                                       body: JSON.stringify({ code }), signal: AbortSignal.timeout(10000) });
  } catch (error) {
    console.error(T(`连不上 ${address}：${error.message}。检查 IP、worker 的防火墙，以及两台电脑是否在同一个网络。`,
                    `Cannot reach ${address}: ${error.message}. Check the IP, the worker's firewall, and that both computers share a network.`));
    process.exit(3);
  }
  const answer = await res.json().catch(() => ({}));
  if (res.status === 404) {
    console.error(T("这台 worker 现在没有打开配对窗口（配对码过期、已经用过，或输错太多次）。在 worker 上运行 pair.ps1 生成新的配对码。",
                    "That worker has no pairing window open (the code expired, was used, or had too many wrong tries). Run pair.ps1 on the worker for a new code."));
    process.exit(3);
  }
  if (!res.ok) {
    console.error(answer.window_open
      ? T("配对码不对，请再核对一下 worker 上显示的数字。", "Wrong pairing code; check the digits the worker shows.")
      : T("配对码不对，输错次数太多，这个配对码已经作废。在 worker 上运行 pair.ps1 生成新的。", "Wrong pairing code, and too many wrong tries closed the window. Run pair.ps1 on the worker for a new code."));
    process.exit(2);
  }
  // A worker without a LAN or Tailscale address (e.g. paired on this computer) is reached where we found it.
  if (!answer.tailscale_url && !answer.lan_url) answer.lan_url = `http://${address}/mcp`;
  addComputer(answer, false, "pair");
  out({ computer: answer.name, manager_name: registry.computers[answer.name]?.manager_name ?? null, main_agent_id: answer.main_agent_id ?? null,
        main_agent_name: answer.main_agent_name ?? null, url: registry.computers[answer.name]?.url });
  process.exit(0);
}
if (cmd === "computers") {
  out(Object.fromEntries(Object.entries(registry.computers).map(([n, pc]) => [n, { description: pc.description, manager_name: pc.manager_name ?? null,
    main_agent_id: pc.main_agent_id ?? null, main_agent_name: mainAgentName(pc), agents: Object.keys(pc.agents) }])));
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
// One read-only board for every registered computer, served on this computer only. The page never sees a
// worker token: this process adds it when it forwards each worker's own dashboard and its /api reads.
// Opening it needs the one-time key in the printed link (kept in a cookie); the Host check stops DNS rebinding.
if (cmd === "dashboard") {
  const { createServer } = await import("node:http");
  const { randomBytes, timingSafeEqual } = await import("node:crypto");
  const key = randomBytes(24).toString("hex");
  const API = new Set(["/api/tasks", "/api/profile", "/api/transcript", "/api/notifications"]);
  // Re-read the registry so computers paired while the board is open show up.
  const computers = () => (existsSync(registryPath) ? JSON.parse(readFileSync(registryPath, "utf8")).computers ?? {} : {});
  const workerGet = (target, path) => fetch(endpoint(target).replace(/\/mcp\/?$/, "") + path, {
    headers: { Authorization: `Bearer ${readFileSync(target.token_file, "utf8").trim()}` }, cache: "no-store", signal: AbortSignal.timeout(10000) });
  const send = (res, status, type, body, extra = {}) => {
    res.writeHead(status, { "Content-Type": type, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                            "Content-Security-Policy": "frame-ancestors 'self'", ...extra });
    res.end(body);
  };
  const sameKey = (value) => typeof value === "string" && value.length === key.length && timingSafeEqual(Buffer.from(value), Buffer.from(key));
  async function status() {
    return Promise.all(Object.entries(computers()).map(async ([name, target]) => {
      const row = { name, label: target.manager_name || name, manager_name: target.manager_name ?? null, main_agent_name: target.main_agent_name || MAIN_AGENT };
      try {
        const res = await workerGet(target, "/api/profile");
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const profile = await res.json();
        return { ...row, online: true, busy: profile.busy ?? null, agents: (profile.agents ?? []).length };
      } catch (error) {
        return { ...row, online: false, error: String(error.message).slice(0, 160) };
      }
    }));
  }
  const server = createServer(async (req, res) => {
    try {
      const port = server.address().port, here = new URL(req.url, `http://127.0.0.1:${port}`);
      if (![`127.0.0.1:${port}`, `localhost:${port}`].includes(req.headers.host) || req.method !== "GET") return send(res, 403, "text/plain", "forbidden");
      if (here.pathname === "/" && here.searchParams.has("k")) {
        if (!sameKey(here.searchParams.get("k"))) return send(res, 403, "text/plain", "forbidden");
        return send(res, 302, "text/plain", "", { Location: "/", "Set-Cookie": `fleet_board=${key}; HttpOnly; SameSite=Strict; Path=/` });
      }
      const cookie = /(?:^|;\s*)fleet_board=([0-9a-f]+)/.exec(req.headers.cookie ?? "")?.[1];
      if (!sameKey(cookie)) return send(res, 403, "text/plain; charset=utf-8", T("请用启动看板时给出的链接打开。", "Open the board with the link printed when it started."));
      if (here.pathname === "/") return send(res, 200, "text/html; charset=utf-8", boardPage());
      if (here.pathname === "/fleet/status") return send(res, 200, "application/json", JSON.stringify(await status()));
      const match = /^\/w\/([^/]+)(\/.*)$/.exec(here.pathname);
      const name = match && decodeURIComponent(match[1]), target = name && Object.hasOwn(computers(), name) ? computers()[name] : null;
      if (!target) return send(res, 404, "text/plain", "not found");
      if (match[2] === "/dashboard") {
        const page = await (await fetch(endpoint(target).replace(/\/mcp\/?$/, "") + "/dashboard", { signal: AbortSignal.timeout(10000) })).text();
        // The worker page calls /api/... with its own token; route those calls through this board instead.
        const shim = `<script>(()=>{const p=${JSON.stringify(`/w/${encodeURIComponent(name)}`)};const f=window.fetch.bind(window);`
          + `window.fetch=(u,o)=>f(typeof u==='string'&&u.startsWith('/api/')?p+u:u,o);try{localStorage.setItem('codexTasksToken','via-orchestrator')}catch{}})();`
          + `</script><style>#logout{display:none!important}</style>`;
        return send(res, 200, "text/html; charset=utf-8", page.replace(/<head>/i, (h) => h + shim));
      }
      if (API.has(match[2])) {
        const upstream = await workerGet(target, match[2] + here.search);
        return send(res, upstream.status, "application/json", await upstream.text());
      }
      return send(res, 404, "text/plain", "not found");
    } catch (error) {
      send(res, 502, "application/json", JSON.stringify({ error: String(error.message).slice(0, 200) }));
    }
  });
  const first = Number(option("port") ?? 8790);
  for (let port = first; ; port++) {
    try {
      await new Promise((ok, fail) => { server.once("error", fail); server.listen(port, "127.0.0.1", () => { server.off("error", fail); ok(); }); });
      break;
    } catch (error) {
      if (error.code !== "EADDRINUSE" || port >= first + 10) throw error;
    }
  }
  const link = `http://127.0.0.1:${server.address().port}/?k=${key}`;
  if (flag("--no-open")) console.log(link);
  else {
    try {
      if (process.platform === "win32") execFileSync("cmd", ["/c", "start", "", link], { stdio: "ignore" });
      else execFileSync(process.platform === "darwin" ? "open" : "xdg-open", [link], { stdio: "ignore" });
    } catch { console.log(link); }
  }
  console.error(T(`机群看板已打开：${link}\n只在这台电脑上能看；关掉这个命令就停止。`,
                  `Fleet board is open: ${link}\nOnly this computer can see it; stop this command to close it.`));
  await new Promise(() => {});
}
function boardPage() {
  const text = {
    title: T("机群看板", "Fleet board"), empty: T("还没有登记任何电脑。用 discover / pair 接入 worker。", "No computers registered yet. Connect a worker with discover / pair."),
    idle: T("空闲", "Idle"), offline: T("离线", "Offline"), running: T("运行", "running"), queued: T("排队", "queued"),
    pick: T("选择左边的一台电脑", "Pick a computer on the left"),
  };
  return `<!doctype html><html lang="${LANG === "en" ? "en" : "zh-CN"}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${text.title}</title><style>
:root{color-scheme:dark;--bg:#161616;--panel:#1e1e1e;--line:#2c2c2c;--text:#ececec;--dim:#9a9a9a;--ok:#4caf7a;--busy:#e0a63a;--off:#666}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.app{display:flex;height:100%}aside{width:260px;flex:none;border-right:1px solid var(--line);background:var(--panel);overflow:auto}
h1{font-size:15px;margin:0;padding:14px 16px;border-bottom:1px solid var(--line)}
.pc{display:flex;gap:10px;align-items:center;width:100%;padding:12px 16px;border:0;border-bottom:1px solid var(--line);background:none;color:inherit;text-align:left;cursor:pointer;font:inherit}
.pc:hover{background:#252525}.pc.on{background:#2a2f3a}.dot{width:9px;height:9px;border-radius:50%;flex:none;background:var(--off)}
.dot.ok{background:var(--ok)}.dot.busy{background:var(--busy)}.name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sub{color:var(--dim);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.meta{min-width:0}
main{flex:1;min-width:0;display:flex}iframe{flex:1;border:0;background:var(--bg)}.hint{margin:auto;color:var(--dim);padding:24px;text-align:center}
@media (max-width:720px){.app{flex-direction:column}aside{width:auto;max-height:40%;border-right:0;border-bottom:1px solid var(--line)}}
</style></head><body><div class="app"><aside><h1>${text.title}</h1><div id="list"></div></aside><main id="main"><div class="hint">${text.pick}</div></main></div>
<script>
const TEXT=${JSON.stringify(text)};let rows=[];let picked=null;try{picked=localStorage.getItem('fleetBoardPick')}catch{}
const $=(id)=>document.getElementById(id);
function show(name){picked=name;try{localStorage.setItem('fleetBoardPick',name)}catch{}
  const f=document.createElement('iframe');f.src='/w/'+encodeURIComponent(name)+'/dashboard';f.title=name;$('main').replaceChildren(f);render()}
function render(){const list=$('list');list.replaceChildren();
  if(!rows.length){const d=document.createElement('div');d.className='hint';d.textContent=TEXT.empty;list.append(d);return}
  for(const r of rows){const b=document.createElement('button');b.className='pc'+(r.name===picked?' on':'');b.onclick=()=>show(r.name);
    const dot=document.createElement('span');const busy=r.online&&r.busy&&(r.busy.running?.length||r.busy.queued);
    dot.className='dot'+(r.online?(busy?' busy':' ok'):'');const m=document.createElement('span');m.className='meta';
    const n=document.createElement('div');n.className='name';n.textContent=r.label;const s=document.createElement('div');s.className='sub';
    s.textContent=!r.online?TEXT.offline:busy?TEXT.running+' '+(r.busy.running?.length||0)+' · '+TEXT.queued+' '+(r.busy.queued||0):TEXT.idle;
    if(r.label!==r.name)s.textContent+=' · '+r.name;m.append(n,s);b.append(dot,m);list.append(b)}}
async function refresh(){try{rows=await (await fetch('/fleet/status',{cache:'no-store'})).json()}catch{}
  render();if(!document.querySelector('iframe')&&rows.length)show(rows.some(r=>r.name===picked)?picked:rows[0].name)}
refresh();setInterval(refresh,10000);
</script></body></html>`;
}
const pc = registry.computers[computer];
if (!pc) {
  const known = Object.entries(registry.computers).map(([n, c]) => (c.manager_name ? `${n}（${c.manager_name}）` : n));
  console.error(T(`未知电脑: ${computer}。已登记: ${known.join(", ")}`, `Unknown computer: ${computer}. Registered: ${known.join(", ")}`));
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

// Named sessions the dispatcher knows about: newest task per name (newest is the one that is continued).
async function namedSessions() {
  const { tasks } = await call("list_tasks", { limit: 100 });
  const byName = new Map();
  for (const t of tasks) {
    if (!t.name || !t.thread_id || t.archived) continue; // archived in the Codex app = removed by the user
    const entry = byName.get(t.name) ?? { latest: t, sessions: new Set(), status: t.status };
    entry.sessions.add(t.thread_id);
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
      if (!/pending work|Wait for the task to finish/.test(error.message)) throw error;
      if (i === 0) console.error(T("  对方正在处理别的事（例如子任务完成后的自动通知），排队等它空闲...",
                                   "  The agent is busy (e.g. with an automatic notice about a finished sub-task); waiting for it..."));
      await sleep(5000);
    }
  }
  throw new Error(T("等了 30 分钟对方仍在忙", "Still busy after 30 minutes"));
}

// The registry follows the worker's main agent by its id: a new name or management name is picked up here.
function syncMain(main) {
  if (pc.main_agent_id && pc.main_agent_id !== main.id) {
    console.error(T(`注意：${computer} 的主 Agent ID 变了（重新安装过？），改用新的主 Agent。`,
                    `Note: ${computer}'s main agent id changed (reinstalled?); using the new main agent.`));
  }
  pc.main_agent_id = main.id;
  const oldName = mainAgentName(pc);
  if (main.name && main.name !== oldName) {
    if (Object.hasOwn(pc.agents, oldName)) { pc.agents[main.name] = { ...pc.agents[oldName], ...(pc.agents[main.name] ?? {}) }; delete pc.agents[oldName]; }
    pc.main_agent_name = main.name;
  }
  if (!pc.manager_name && main.manager_name && !managerNameConflict(main.manager_name, computer)) pc.manager_name = main.manager_name;
}

async function talkMain(prompt) {
  const main = (await call("get_profile", {})).main_agent;
  if (!main) return talkNamed(mainAgentName(pc), prompt); // workers from before main agent ids
  syncMain(main);
  if (pc.manager_name !== (main.manager_name ?? undefined)) await call("update_computer_profile", { manager_name: pc.manager_name ?? "" });
  const name = mainAgentName(pc);
  const started = main.task_id ? await continueWhenFree(main.task_id, prompt)
    : await call("submit_task", { prompt, name, ...(main.workspace ? { workspace: main.workspace } : {}) });
  return report(name, started, (pc.agents[name] ??= {}));
}

function talk(agentName, prompt) {
  return agentName === mainAgentName(pc) || (pc.manager_name && agentName === pc.manager_name) ? talkMain(prompt) : talkNamed(agentName, prompt);
}

async function talkNamed(agentName, prompt) {
  let agent = pc.agents[agentName];
  let started;
  if (agent?.last_task_id) {
    started = await continueWhenFree(agent.last_task_id, prompt);
  } else {
    // Look for an existing session even when the agent is in the registry: a registered agent can't get a second one.
    const found = (await namedSessions()).get(agentName);
    if (found?.latest) {
      if (found.sessions.size > 1) console.error(T(`注意：有 ${found.sessions.size} 个叫「${agentName}」的会话，使用最近的一个。`,
                                                   `Note: ${found.sessions.size} sessions are named "${agentName}"; using the newest.`));
      started = await continueWhenFree(found.latest.task_id, prompt);
      agent = pc.agents[agentName] = { ...(agent ?? {}) };
    } else if (agent || flag("--new")) {
      const workspace = option("workspace") ?? agent?.workspace;
      started = await call("submit_task", { prompt, name: agentName, ...(workspace ? { workspace } : {}) });
      agent = pc.agents[agentName] = { ...(agent ?? {}), ...(workspace ? { workspace } : {}) };
    } else {
      const names = [...new Set([...Object.keys(pc.agents), ...(await namedSessions()).keys()])];
      console.error(T(`电脑 ${computer} 上没有叫「${agentName}」的 Agent。可用: ${names.join("、") || "（无）"}。要新建请加 --new。`,
                      `No agent named "${agentName}" on ${computer}. Available: ${names.join(", ") || "(none)"}. Add --new to create it.`));
      process.exit(3);
    }
  }
  return report(agentName, started, agent);
}

async function report(agentName, started, agent) {
  console.error(T(`已发给 ${computer} 的「${agentName}」（任务 ${started.task_id}），等待回复...`,
                  `Sent to "${agentName}" on ${computer} (task ${started.task_id}); waiting for the reply...`));
  const done = await wait(started.task_id);
  if (done.status === "completed") {
    Object.assign(agent, { last_task_id: done.task_id, thread_id: done.thread_id });
    saveRegistry();
  }
  const locked = /held by another Codex window|桌面 App 里打开过/.test(done.error || "");
  out({ computer, ...(pc.manager_name ? { manager_name: pc.manager_name } : {}), agent: agentName, task_id: done.task_id,
        thread_id: done.thread_id, status: done.status, reply: done.result, error: done.error,
        ...(locked ? { hint: T("这个会话在 Codex App 里被打开过，被 App 锁住了。彻底退出 Codex App 后再试，或让主 Agent 另开一个任务。",
                               "This session was opened in the Codex app, which keeps it locked. Quit the Codex app completely and retry, or let the main agent start a new task.") } : {}) });
}

await connect(pc);
const list = (value) => value?.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
const toggle = (name) => {
  const value = option(name)?.toLowerCase();
  if (value === undefined) return undefined;
  if (["on", "true", "yes", "1"].includes(value)) return true;
  if (["off", "false", "no", "0"].includes(value)) return false;
  throw new Error(`--${name} must be on or off`);
};
// Normally unset: Codex's default model, like a new window.
const modelChoice = () => Object.fromEntries(["model", "effort"].filter((k) => option(k)).map((k) => [k, option(k)]));

switch (cmd) {
  case "agents": {
    const sessions = await namedSessions();
    const described = Object.fromEntries((await call("get_profile", {})).agents.map((a) => [a.name, a]));
    const names = [...new Set([...Object.keys(described), ...Object.keys(pc.agents), ...sessions.keys()])];
    out(names.map((n) => ({ agent: n, default: n === mainAgentName(pc), role: described[n]?.role ?? null, tags: described[n]?.tags ?? [],
                            can_dispatch: described[n]?.can_dispatch ?? true, locked: described[n]?.locked ?? false,
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
    out(await call("register_agent", { name: rest[0], role: option("role"), tags: list(option("tags")), agent_type: option("type") ?? (rest[0] === mainAgentName(pc) ? "main" : "resident"),
                                       can_dispatch: toggle("can-dispatch"), locked: toggle("locked") }));
    break;
  case "remove-agent":
    out(await call("remove_agent", { name: rest[0] }));
    break;
  case "agent":
    await talk(rest[0], rest.slice(1).join(" "));
    break;
  case "export":
    await talkMain(rest.join(" "));
    break;
  case "set-manager-name": {
    const name = (rest[0] ?? "").trim();
    const taken = name && managerNameConflict(name, computer);
    if (name && (!validAgentName(name) || taken)) {
      console.error(T(`管理名「${name}」无效${taken ? `，已经被 ${taken} 用了` : ""}。`, `The management name "${name}" is invalid${taken ? `; ${taken} already uses it` : ""}.`));
      process.exit(2);
    }
    if (name) pc.manager_name = name; else delete pc.manager_name;
    await call("update_computer_profile", { manager_name: name });
    const main = (await call("get_profile", {})).main_agent;
    if (main) syncMain(main);
    saveRegistry();
    out({ computer, manager_name: pc.manager_name ?? null, main_agent_id: pc.main_agent_id ?? null, main_agent_name: mainAgentName(pc) });
    break;
  }
  case "tools":
    out((await rpc("tools/list", {})).tools.map((t) => t.name));
    break;
  case "submit": {
    const name = option("name");
    const started = await call("submit_task", { prompt: rest.join(" "), ...(name ? { name } : {}), ...modelChoice() });
    out(flag("--wait") ? await wait(started.task_id) : started);
    break;
  }
  case "continue":
    out(await call("continue_task", { task_id: rest[0], prompt: rest.slice(1).join(" "), ...modelChoice() }));
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
  case "cancel":
    for (const taskId of rest) out(await call("cancel_task", { task_id: taskId }));
    break;
  default:
    console.error(T("未知命令，见文件开头的用法说明", "Unknown command; see the usage at the top of this file"));
    process.exit(2);
}
