# agent-fleet

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
| `orchestrator/` | 管理端电脑 | `fleet.mjs` 命令行客户端、`/call-agent` skill 模板 |
| `install-orchestrator.ps1` | 管理端电脑 | 安装 skill，创建登记表目录 |

每台电脑自己的配置、令牌和数据在 `%LOCALAPPDATA%\agent-fleet\`；管理端的登记表和各电脑令牌在 `~/.agent-fleet/`。这些都**不在**项目文件夹里，不会进 git。

## 要求

- Windows 10/11（目前只在 Windows 上测试过；Mac 需要另写启动和硬件检测部分）
- [Tailscale](https://tailscale.com)：所有电脑登录同一个账号，组成内网
- Codex：用你的 ChatGPT 账号登录（`codex login`）
- 管理端需要 [Claude Code](https://claude.com/claude-code) 和 Node.js

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

在运行 Claude Code 的电脑上：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-orchestrator.ps1
node orchestrator\fleet.mjs add-computer <那台电脑的 connection.json>
```

接入后，`connection.json` 可以删掉，令牌已经保存在 `~/.agent-fleet/tokens/`。如果管理端自己也是一台被管理的电脑，加 `--local=1`，让它走本机地址。

## 使用

在 Claude Code 里：

```
/call-agent 【pc-1】【主agent export】帮我看看 B站有没有新私信
/call-agent 【pc-1】                         列出这台电脑上的 Agent
```

或者直接说“列出所有电脑和 Agent”“找一台有 GPU 的电脑剪视频”。

常用命令（`node orchestrator\fleet.mjs …`）：

| 命令 | 作用 |
|---|---|
| `overview` | 所有电脑的档案总览 |
| `agents <电脑>` | 这台电脑上能对话的 Agent 和它们的职责 |
| `agent <电脑> "<Agent名>" "<消息>"` | 和某个 Agent 对话（续接它的会话，等回复） |
| `watch` | 持续输出所有电脑的新通知（给后台监视用） |
| `notifications <电脑>` | 读通知全文 |
| `profile <电脑> [--refresh]` | 一台电脑的完整档案，`--refresh` 重新检测硬件 |

只读看板：`http://<电脑的 Tailscale 名字>:8766/dashboard`，第一次打开时粘贴那台电脑的令牌。

## 必须知道的几件事

- **令牌等于这台电脑的控制权**。用 `-FullAccess` 时，持有令牌并能连到服务的人可以让电脑做任何事。令牌只放在自己的电脑上，不要发给别人；Tailscale 账号也不要共享。
- **不要在 Codex 桌面 App 里点开 Agent 正在用的会话**。App 会一直锁住它打开过的会话，直到 Codex 完全退出，这期间 Agent 无法继续那个会话。想看进度，用只读看板。
- **派任务服务一次只运行一个任务**，避免多个任务同时抢鼠标键盘。主agent export 派完任务就结束本轮，等子 agent 做完的自动通知。
- **子 agent 被明确要求不碰派任务服务和令牌**。这是写给它们的规则，不是技术上做不到，因为 `-FullAccess` 下子 agent 能读到电脑上的任何文件。

## 开发

```powershell
cd worker
<python> -m unittest test_dispatcher.py
```
