---
name: codex-quota-guard
description: Read the signed-in Codex account's quota windows (5-hour and weekly) and guard multi-step work against running out. When quota is tight it denies expensive tool calls and forces a resumable handoff document (`.handoff/<id>/HANDOFF.md`) before stopping, so another agent or a later session can pick the task up. Use at the start of any multi-step or long-running coding task, before expensive commands (install, build, test, deploy, git push), after each completed milestone, when a rate-limit or 429 error appears, and whenever asked about remaining usage, reset times, writing or resuming a handoff, or handing a task to another agent. Not for OpenAI API-key rate limits.
---

# Codex 配额护栏

额度、判定、交接三件事都在这一个技能里。hooks 会自动维护状态，**不要手改 gate 文件**。

状态文件：`~/.codex/quota-guard/gate.json`（唯一真源）
脚本：`python3 ~/.agents/skills/codex-quota-guard/scripts/quota_guard.py`

## 1. 取数：别急着起 app-server

```bash
python3 .../quota_guard.py status            # 读缓存/rollout，毫秒级
python3 .../quota_guard.py status --refresh  # 强制重取
python3 .../quota_guard.py status --fresh    # 直接问 app-server，不读 rollout
```

数据源优先级：当前会话 rollout 文件尾部的 `rate_limits`（零成本）→ `codex app-server` 的
`account/rateLimits/read`（1~3 秒，仅在 rollout 缺失、过期或账号对不上时用）。
`status` 的退出码就是判定：`0 OK`、`10 TIGHT`、`11 CRITICAL`、`12 EXHAUSTED`、`13 UNKNOWN`。

**切换账号**：额度是「账号 + 时间窗」的函数，换了登录就等于换了额度。护栏用
`~/.codex/auth.json` 的 `tokens.account_id`（以及 `/mnt/c/Users/*/.codex/auth.json`）认账号，
和 `gate.json` 里记的上一个账号比对：

- 账号变了 → 之前缓存的额度、以及 rollout 里切换前写下的那行 `rate_limits` 一律作废，
  直接改问 app-server（它返回的 `accountId` 才是权威归属），不受 `cache_ttl_seconds` 限制。
- 新账号还没写过额度行、app-server 又问不到时，判定降级成 `UNKNOWN`，`status` 和注入的护栏块
  都会写明「额度仍是上一个账号的，待刷新」。这时**不要**拿这组数字判断能不能做完；
  它是上一个账号的，通常会偏乐观或偏悲观。
- 新账号写出第一行额度后自动恢复，不需要重开会话。
- `status` 自己会先认账号：`auth.json` 的号和 `gate.json` 里记的不一样就直接重读，
  不用等 `--refresh`，也不用等缓存过期。
- `status` 最后一行 `账号:` 是 8 位短 id，可用于确认现在读的是谁。两个 home
  （`~/.codex` 与 `/mnt/c/Users/*/.codex`）可能各登一个号，此时若数据来自另一个已登录账号，
  会标注 `（额度来自 xxxxxxxx）`——这是真实额度，可以照常判断，只是不是当前 auth.json 那个号。

窗口口径：`primary` = 5 小时（300 分钟），`secondary` = 周（10080 分钟）。只有
`window_minutes == 300` 才能叫「5 小时」，别按顺序猜。

## 2. 判定口径

| 判定 | 含义 | 你该做什么 |
|---|---|---|
| `OK` | 剩余充足 | 正常推进 |
| `TIGHT` | 5h 剩 < 15% 或周剩 < 10% | 不扩范围：不重构、不加依赖、不顺手改无关文件 |
| `CRITICAL` | 5h 剩 ≤ 10%（或按 burn rate 算出做不完） | 停止任务，只做现场采集和交接 |
| `EXHAUSTED` | 剩余 ≤ 0.5%，或 Codex 报限额/429 | 立刻停，不再派发工具调用，先写交接 |
| `WAIT_RESET` | 重置在即，等更划算 | 挂起到 `resets_at` 之后继续（需 `wait_reset.enabled: true`） |

百分比只是估计，**报错是事实**：任何 `rate_limit_reached` / 429 直接按 `EXHAUSTED` 处理。

## 3. 检查时机

| 时机 | 谁触发 | 动作 |
|---|---|---|
| 会话开始 / resume / compact | hook | 自动注入护栏，无需你操作 |
| 用户新 prompt | hook | 判定异常时注入警告（同等级只提醒一次） |
| 多步任务开工前 | **你** | `quota_guard.py plan --items <待办项数>`，让脚本判一次可行性 |
| 每 8 次 Bash / 10 次模型请求 | hook | 自动刷新；进入 TIGHT 后每次本地工具调用前轻量刷新 |
| 昂贵命令前 | hook | `PreToolUse` 会拦；被拦时先写交接，或把命令拆小 |
| 每个里程碑完成 | **你** | `quota_guard.py check --milestone` |
| 每次本地工具调用前 | hook | CRITICAL/EXHAUSTED 只放行现场采集和交接，并自动生成交接草稿 |
| 每轮结束 | hook | `Stop` 会在 CRITICAL 且交接仍有 TODO 时阻止结束，逼你写完 |

`plan` 之后脚本会用 burn rate（`%/轮`）估 `r5 - reserve - burn × est_turns`，负数就是做不完。
待办项数变了要重跑 `plan`。

**版本差异**：`PostModelRequest` 只有新版 codex 才触发——standalone CLI 0.154.0 里没有这个事件，
桌面版（0.167 WSL 桥）和 VSCode 扩展（0.169）都有，且它们共用同一个 `CODEX_HOME`，
所以这个 home 一般能收到。纯 CLI 收不到时「模型请求」计数会停住，这不影响护栏——额度刷新
由「每 8 次 Bash」和每轮 `Stop` 兜住，TIGHT 时每次本地工具调用前还会轻量刷新。
看到计数不动不必排查。

## 4. 暂停协议

判定进入 `CRITICAL` / `EXHAUSTED` 时，hook 会先生成可恢复的交接草稿并拦住普通工具；按顺序做，不许跳步：

1. **停手**：不再派发工具调用，不开新子任务，不回滚已完成改动。
2. **采现场**（`handoff` 命令会自动做 git 部分）：
   ```bash
   git status --porcelain && git diff --stat
   ```
3. **生成交接骨架**：
   ```bash
   python3 .../quota_guard.py handoff --goal "<一句话目标>"
   ```
   它落在 `<repo>/.handoff/<utc>-<slug>/`，含 `HANDOFF.md` + `handoff.json`，
   并自动写 `.git/info/exclude`（本地忽略，不进版本库）。
4. **填 9 段 TODO**：目标与验收、已完成（带证据）、未完成（有序，含 entry/expect/verify）、
   关键决策、坑、环境复现、下一步第一动作、待确认问题、resume prompt。
   留一个 TODO 没填，接手方就得重新推一遍，交接等于白写。
5. **输出 6 行暂停摘要**：原因 / 剩余额度 / 交接路径 / 建议接手方式 / 下一步第一动作 / resume 命令。

禁止：伪造完成、悄悄缩范围后交付、把未完成项写成「已实现，待验证」。

`WAIT_RESET` 不写交接，直接说明等到几点，然后停。

## 5. 交接给谁

- 同机另一个 agent：它读 `.handoff/<id>/HANDOFF.md` 即可，内容自包含。
- 需要可验收的任务单：转 `codex-cooperation` 的 `handoffctl create`（可选，不在本技能范围）。

## 6. 异常处理

- 脚本报 `codex executable was not found` / 账号不是 Codex 服务：说明具体错误，建议
  `codex login status`、`codex login` 或交互式 `/status`。
- 拿不到额度（判定 `UNKNOWN`）：**不要停**，继续干活但避免昂贵命令；记一笔日志即可。
  测量失败不该阻断工作。
- 永远不要为了「刷新额度」消耗 rate-limit reset credit —— 除非用户明确要求。
