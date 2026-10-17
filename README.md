# agent-fleet

**Agent Fleet 是一个插件**，可安装到 Codex 或 Claude Code。运行 setup 后选择这台电脑的角色：orchestrator、worker 或 both；worker 可选择 Codex 或 Claude Code 引擎。安装、权限与验证说明见 [PLUGINS.md](PLUGINS.md)。

用一个 AI Agent 管理多台电脑上的 AI Agent。不重新开发 Agent，而是在现成的 Codex 之上，加一层跨电脑的通信、调度和管理。

```
你（手机 / 电脑）
  └→ orchestrator agent（Claude Code，在管理端电脑上）
       └→ 每台电脑的「主agent export」（Codex，负责这台电脑）
            └→ 这台电脑上的子 agent（Codex，真正干活：操作桌面、浏览器、软件）
```

- **主agent export 有自主权**：orchestrator 只给目标，由它决定自己做、派一个子 agent，还是拆成几个。
- **子 agent 做完会自动叫醒主agent export**，它接着推进；整件事完成后，主动通知 orchestrator，orchestrator 再告诉你。
- **每台电脑有档案**：硬件（自动检测一次）、用途、软件、账号登录状态，以及常驻 Agent 和它们的职责。
- **只读看板**：在浏览器或手机上实时查看每台电脑的会话和进度，不会打扰正在工作的 Agent。

## 组成

| 目录 / 文件 | 装在哪里 | 作用 |
|---|---|---|
| `worker/` | 每台被管理的电脑 | 派任务服务（MCP over HTTP）、只读看板、主agent export 的说明模板 |
| `install-worker.ps1` | 每台被管理的电脑 | 一键安装 worker |
| `plugin/` | 管理端电脑 | Claude Code 插件：`/call-agent` skill 和它自带的 `fleet.mjs` 命令行客户端 |
| `install-orchestrator.ps1` | 管理端电脑 | 不用插件市场时，手动安装 skill（也可以装到 Codex） |
| `AGENTS.md` / `CLAUDE.md` | — | 写给 AI 的安装和开发说明 |

每台被管理的电脑上有一个工作区，默认是 `%USERPROFILE%\AgentFleet\`，可以用 `-Workspace` 改：

```
AgentFleet\
  main-agent\    主agent export 的工作目录
  tasks\         每个子任务一个文件夹：日期_任务名_编号
  shared\        子任务之间交接文件、给你的最终产出
```

内部数据（配置、令牌、任务记录、档案、通知）在 `%LOCALAPPDATA%\agent-fleet\`；管理端的登记表和各电脑令牌在 `~/.agent-fleet/`。这些都**不在**项目文件夹里，不会进 git。

## 要求

- Windows 10/11（目前只在 Windows 上测试过；Mac 需要另写启动和硬件检测部分）
- [Tailscale](https://tailscale.com)：所有电脑登录同一个账号，组成内网
- Codex：用你的 ChatGPT 账号登录（`codex login`）
- 管理端需要 [Claude Code](https://claude.com/claude-code) 和 Node.js

## 最省事的方法：让 AI 帮你装

克隆本项目后，在项目文件夹里打开 Claude Code 或 Codex，说：

> 帮我把这台电脑装成 agent-fleet 的 worker（或管理端）。

AI 会按 `AGENTS.md` 里的步骤一步步做，遇到登录 Tailscale、登录 Codex 这些必须你本人操作的地方会停下来提醒你。下面是手动步骤。

## 安装一台被管理的电脑

在那台电脑上，用平时登录的 Windows 用户（不需要管理员）：

```powershell
git clone <本项目地址> agent-fleet
cd agent-fleet
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-worker.ps1 -FullAccess -Description "视频剪辑机，有 GPU"
```

脚本会安装缺少的 Node.js、Python、Tailscale 和 Codex 命令行，部署派任务服务，生成令牌，创建这台电脑的主agent export，设置登录后自动启动，最后生成 `%LOCALAPPDATA%\agent-fleet\connection.json`。

只有两件事要人来做：**登录 Tailscale**，**登录 Codex**。没登录的话脚本会提醒，登录后再运行一次即可。

`-FullAccess`：子 agent 不设沙箱，并自动同意权限请求，才能操作桌面、浏览器和其他程序。不加的话，子 agent 只能读写自己的工作目录。

## 安装管理端

在运行 Claude Code 的电脑上，从插件市场安装（推荐）：

```
/plugin marketplace add cxfydhcg/agent-fleet
/plugin install agent-fleet@agent-fleet
```

装好后 skill 叫 `/agent-fleet:call-agent`。也可以不用插件市场，克隆项目后运行 `install-orchestrator.ps1`，装成 `/call-agent`；加 `-Codex` 同时装到 Codex 里，Codex 也能用同一个 skill。

然后接入每台被管理的电脑，可以让 Claude 帮你运行，也可以自己运行 skill 文件夹里的客户端：

```powershell
node <skill 文件夹>\fleet.mjs add-computer <那台电脑的 connection.json>
```

接入后，`connection.json` 可以删掉，令牌已经保存在 `~/.agent-fleet/tokens/`。如果管理端自己也是一台被管理的电脑，加 `--local=1`，让它走本机地址。

## 使用

在 Claude Code 里：

```
/agent-fleet:call-agent 【pc-1】【主agent export】帮我看看 B站有没有新私信
/agent-fleet:call-agent 【pc-1】              列出这台电脑上的 Agent
```

（手动安装的话命令是 `/call-agent`。）也可以直接说“列出所有电脑和 Agent”“找一台有 GPU 的电脑剪视频”。

常用命令（`node <skill 文件夹>\fleet.mjs …`）：

| 命令 | 作用 |
|---|---|
| `overview` | 所有电脑的档案总览 |
| `agents <电脑>` | 这台电脑上能对话的 Agent 和它们的职责 |
| `agent <电脑> "<Agent名>" "<消息>"` | 和某个 Agent 对话（续接它的会话，等回复） |
| `watch` | 持续输出所有电脑的新通知（给后台监视用） |
| `notifications <电脑>` | 读通知全文 |
| `profile <电脑> [--refresh]` | 一台电脑的完整档案，`--refresh` 重新检测硬件 |
| `describe-agent <电脑> "<Agent名>" --role=职责 [--can-dispatch=on\|off] [--locked=on\|off]` | 登记 Agent；`--can-dispatch=off` 不让它开新会话，`--locked=on` 只有 orchestrator 能续接/取消/打开它的会话 |

**会话归属**：主 Agent 用 `submit_task` 开的会话归它所有，`continue_task` / `cancel_task` / `open_task` 只对自己开的会话（及其续接）有效；orchestrator 能控制所有会话。`can_dispatch` 和 `locked` 只有 orchestrator 能设置，主 Agent 也不能删掉带这些限制的登记。这些限制针对派任务服务能认出来的主 Agent 调用（Claude 引擎靠每轮的请求头，Codex 靠 turn metadata）；认不出调用者的请求按 orchestrator 处理。它们是管理规则，不是安全边界：拿到令牌的人仍然拥有全部权限。

**同名会话**：登记过的 Agent（常驻或主 Agent）只保留一个会话：已经有会话时，再用这个名字 `submit_task` 会被拒绝，报错里带着该续接的 `task_id`（`get_profile` 里每个 Agent 也有 `task_id`）。用户在 Codex 桌面 App 里归档了旧会话后才能重新开。没登记的一次性任务可以重名。

只读看板：`http://<电脑的 Tailscale 名字>:8766/dashboard`，第一次打开时粘贴那台电脑的令牌。

## 必须知道的几件事

- **令牌等于这台电脑的控制权**。用 `-FullAccess` 时，持有令牌并能连到服务的人可以让电脑做任何事。令牌只放在自己的电脑上，不要发给别人；Tailscale 账号也不要共享。
- **不要在 Codex 桌面 App 里点开 Agent 正在用的会话**。App 会一直锁住它打开过的会话，直到 Codex 完全退出，这期间 Agent 无法继续那个会话。想看进度，用只读看板。
- **派任务服务一次只运行一个任务**，避免多个任务同时抢鼠标键盘。主agent export 派完任务就结束本轮，等子 agent 做完的自动通知。
- **Codex 额度用完时不开始任务**。每个任务开始前先查 Codex 的 5 小时和每周额度，任一已用完，整个队列按原顺序暂停到额度重置后约 1 分钟（一次最多等 6 小时再查），任务的最新消息和看板上会显示预计时间，并给 orchestrator 发一条通知。查询失败或返回格式看不懂时照常运行。
- **子 agent 被明确要求不碰派任务服务和令牌**。这是写给它们的规则，不是技术上做不到，因为 `-FullAccess` 下子 agent 能读到电脑上的任何文件。

## 可选设置：Codex 额度保护

改 `%LOCALAPPDATA%\agent-fleet\config.json`，重启派任务服务后生效。只对默认的 app-server 方式有效。

| 配置项 | 默认 | 作用 |
|---|---|---|
| `quota_guard` | `true` | 任务开始前查 Codex 额度（app-server 的 `account/rateLimits/read`），用完就让队列等到重置。设成 `false` 关闭。 |
| `quota_guard_threshold` | `100` | 5 小时或每周额度的已用百分比达到这个值就算用完，例如 `95` 留一点余量。 |

## 可选设置：会话在 Codex 桌面 App 里怎么显示

改 `%LOCALAPPDATA%\agent-fleet\config.json`，重启派任务服务后生效。重新运行 `install-worker.ps1` 会重写这个文件（旧的留在 `config.json.bak`），需要再加一次。

| 配置项 | 默认 | 作用 |
|---|---|---|
| `resident_title_suffix` | `" - 固定"` | 登记为常驻（resident）的 Agent，会话标题后面加这个后缀，和一次性任务区分开；主agent export 不加。只改 App 里显示的标题，派任务时还用原名。设成 `""` 关闭。 |
| `codex_project_id` | 不设 | 新会话放进这个 Codex 项目（用 app-server 的实验接口 `projectId`）。不设时行为不变。 |
| `codex_app_folders` | 安装时打开 | 把派任务服务的会话自动放进 Codex 桌面 App 侧边栏的两个文件夹：`main`（默认 `AgentFleet`）放主 Agent 自己和它开的所有会话，`other`（默认 `AgentFleet-其他`）放 orchestrator 或其他 Agent 直接开的会话。常驻和一次性的都在里面，常驻的标题带 `resident_title_suffix`。写成 `{"main": "名字", "other": "名字"}` 改名，`true` 用默认名，`false` 关闭。只对 Codex 引擎有效。 |

目前的桌面 App（26.1007）还不显示 app-server 的项目，侧边栏文件夹记在 App 的**内部文件** `~/.codex/.codex-global-state.json` 里，`codex_app_folders` 就是改这个文件：

- App 运行时会覆盖这个文件，所以**只在 App 关着时放**：任务结束后检查一次（之后每 30 秒），App 开着就等它退出后再放。新会话要等下次打开 App 才出现在文件夹里。
- 两个文件夹按根目录认（`main` 是工作区，`other` 是 `tasks_dir`），在 App 里改了文件夹名字也还认得。你自己拖到别的文件夹的会话不会被移回来。
- 每次写之前备份为 `.codex-global-state.json.bak-agent-fleet`。这个文件格式不公开，App 更新后可能失效；失效时只是不放，任务照常运行，`get_profile` 的 `codex_app_folders` 里有最近一次的结果。

不想让服务自动改，也可以关掉 `codex_app_folders`，手动运行辅助脚本，把所有会话放进一个文件夹：

```powershell
# 先彻底退出 Codex 桌面 App（ChatGPT.exe）；脚本检测到它在运行会拒绝执行
python worker\tools\codex_app_folder.py --config "$env:LOCALAPPDATA\agent-fleet\config.json" --dry-run
python worker\tools\codex_app_folder.py --config "$env:LOCALAPPDATA\agent-fleet\config.json" [--folder-name AgentFleet] [--root <文件夹根路径>]
```

## 开发

```powershell
cd worker
<python> -m unittest test_dispatcher.py
claude plugin validate ..
```

更多约定见 `AGENTS.md`。

## 许可证

MIT
