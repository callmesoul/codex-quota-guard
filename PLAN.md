# Codex 配额护栏 + 自动交接：实现方案

## 0. 结论先行

「让 skill 在所有任务会话里都被调用」这件事，**靠 skill 本身做不到**。

Codex 的 skill 走隐式调用：模型读到 `name + description`，自己决定要不要读全文。
描述写得再宽，仍然是概率事件。实测口径：

| 手段 | 触发可靠性 | 装载时机 | 压缩后是否还在 |
|---|---|---|---|
| SKILL.md description | 概率（估 60~90%） | 任务匹配时 | 可能被驱逐 |
| `~/.codex/AGENTS.md` | 高，但不保证 | 每轮重读 | 在 |
| **hooks（`~/.codex/hooks.json`）** | **100%** | 系统执行，不经模型 | 在 |

所以方案是三层叠加，**确定性逻辑全部下沉到 hooks**，skill 只保留「怎么判断、怎么暂停、交接文档写什么」这套程序性知识。

本机已核实的前提：

- `codex-cli 0.154.0`（WSL，`/home/callmesoul/.local/bin/codex`）
- `codex features list` → `hooks  stable  true`，**不需要再开 feature flag**
- `CODEX_HOME` 实际是 `/mnt/c/Users/callmesoul/.codex`（config 里的 marketplace/projects 都是 `/mnt/c` 与 `/home/callmesoul` 混排）
- `~/.codex/hooks.json` **不存在**，是干净的
- skill 目录是 `$HOME/.agents/skills/`，不是 `~/.codex/skills/`（后者 Codex 不认）
- 本机 rollout 里已有配额快照（见 §2），**能零成本拿额度，不用每次拉起 app-server**

---

## 1. 架构

```
┌──────────────────────────────────────────────────────────────┐
│ L1 确定性层  ~/.codex/hooks.json  →  quota_guard.py hook ...  │
│   SessionStart / UserPromptSubmit / PreToolUse /              │
│   PostToolUse / PostModelRequest / Stop / PreCompact          │
│   职责：读额度、算等级、写 gate.json、必要时 deny / block      │
└───────────────────────────┬──────────────────────────────────┘
                            │ 读写
                 ~/.codex/quota-guard/gate.json  (唯一真源)
                            │ 读
┌───────────────────────────┴──────────────────────────────────┐
│ L2 技能层  SKILL.md（codex-quota-guard）                      │
│   职责：判定口径 / 估算公式 / 暂停协议 / 交接文档 schema       │
└───────────────────────────┬──────────────────────────────────┘
                            │ 引用
┌───────────────────────────┴──────────────────────────────────┐
│ L3 指令层  ~/.codex/AGENTS.md                                  │
│   两三行硬规则 + @ 引用 SKILL.md，保证压缩后仍在               │
└──────────────────────────────────────────────────────────────┘
```

一句话分工：**hooks 负责「何时看、看不下去就拦」，SKILL.md 负责「看到了怎么想」，AGENTS.md 负责「别忘了看」。**

---

## 2. 数据源：优先本地，兜底 app-server

本机 rollout 实测样本（`$CODEX_HOME/sessions/2026/11/11/rollout-*.jsonl`）：

```json
"rate_limits":{"limit_id":"codex","limit_name":null,
 "primary":{"used_percent":24.0,"window_minutes":300,"resets_at":1794382158},
 "secondary":{"used_percent":24.0,"window_minutes":10080,"resets_at":1794550973},
 "credits":{"has_credits":false,"unlimited":false,"balance":null}}
```

注意字段是 **snake_case**，而现有脚本从 app-server 拿到的是 **camelCase**（`usedPercent` / `windowDurationMins`）。两条路要归一化到同一个结构。

| 数据源 | 延迟 | 额度消耗 | 适用 |
|---|---|---|---|
| A. rollout JSONL 尾部 `rate_limits`（当前会话自身） | < 5 ms（尾部 seek 64KB） | 0 | 默认，所有 hook |
| B. `codex app-server` → `account/rateLimits/read`（现有脚本） | 1~3 s | 0 token，但起进程 | 无 rollout / rollout 过期 / 强制刷新 |
| C. `PostModelRequest` 的 `token_usage` | 0 | 0 | 累计 burn，不用于判额度 |

实现要点：A 必须**从文件尾部反向读**，不能全量解析（本机单文件已 441KB，长会话会到几十 MB）。找到最后一条含 `rate_limits` 的行即停。
若 `resets_at < now`，数据视为过期 → `used_percent` 归 0，并用 B 复核一次。

统一快照结构：

```json
{"source":"rollout","captured_at":1762000000,
 "p5":24.0,"r5_at":1794382158,"p7":24.0,"r7_at":1794550973,
 "credits":{"has_credits":false,"unlimited":false,"balance":null},
 "reached_type":null}
```

---

## 3. 判定：什么时候「算做不完」

### 3.1 四级状态（阈值可配）

| 等级 | 条件（`r = 100 - used`） | 行为 |
|---|---|---|
| `OK` | `r5 ≥ 25` 且 `r7 ≥ 15` | 正常干 |
| `TIGHT` | `r5 < 25` 或 `r7 < 15` | 只做已有范围，不开新坑，不给新估算外的重构 |
| `CRITICAL` | `r5 ≤ 10` 或 `r7 ≤ 5` | 停止任务，只放行现场采集与交接 |
| `EXHAUSTED` | `r5 ≤ 0.5` 或 `reached_type != null` 或收到 429 | 立刻停，不再派发任何工具调用 |

`reserve = 3`：给写交接文档本身留的百分比。判 CRITICAL 时按 `r5 - reserve` 算可用。

### 3.2 能不能在窗口内做完

单看百分比不够 —— 20% 可能够跑 30 轮，也可能一轮就烧完。用 burn rate：

```
burn5 = EWMA(Δp5 / Δturn, α = 0.3)        # 本会话历史；无历史用 default 1.0
est_turns = Σ(剩余待办项 × 1.5)            # 或 agent 在计划阶段显式声明的 est_turns
```

判定：

```
if  r5 - reserve - burn5 * est_turns  <  0   →  CANNOT_FINISH
```

再加一条时间维度，避免「明明 8 分钟后重置还硬交接」：

```
if  r5_at - now < 20min  and  est_turns * burn5  < 100   →  WAIT_RESET（可选，默认关）
```

### 3.3 四种结论

| 结论 | 含义 | 谁执行 |
|---|---|---|
| `CONTINUE` | 继续 | —— |
| `TIGHTEN` | 继续，但禁止扩范围 | agent（Prompt 约束） |
| `HANDOFF_AFTER_STEP` | 做完当前步骤就停 | agent |
| `HANDOFF_NOW` | 立即停 | hook 用 `deny` / `block` 强制 |
| `WAIT_RESET` | 挂起到窗口重置（默认关闭） | agent |

**硬信号优先**：一次 `429` / `rate_limit_reached` 直接落 `EXHAUSTED`，不管百分比多少。百分比是估计值，报错是事实。

---

## 4. 检查时机表（核心）

| ID | 时机 | 触发者 | 数据源 | 动作 |
|---|---|---|---|---|
| T0 | 会话开始 / resume / compact / fork | `SessionStart` | A，无则 B | 算等级，写 gate，注入 ≤ 500 字护栏 |
| T1 | 用户每次提交 prompt | `UserPromptSubmit` | gate（TTL 60s） | `EXHAUSTED` → `decision:"block"` 拦新任务；`CRITICAL` → additionalContext 警告一次 |
| T2 | 计划成型（agent 主动） | skill 命令 `plan --items N` | 本地 | 写入 `est_turns`，立刻做一次可行性判定 |
| T3 | 每 8 次 Bash 调用 | `PostToolUse`(Bash) | A | 更新 gate（无感，纯本地） |
| T4 | 每 10 次模型请求 | `PostModelRequest` | `token_usage` + A | 累 burn，重算 |
| T5 | **每次本地工具执行前** | `PreToolUse`(*) | TIGHT 后读 A；昂贵命令必要时读 B | CRITICAL/EXHAUSTED 自动草拟交接并拦普通工具 |
| T6 | 每完成一个里程碑 | agent 主动 `check --milestone` | A | 重算 est_turns |
| T7 | 收到限额错误 | agent 识别 + T3 兜底 | 错误文本 | 立即 `HANDOFF_NOW` |
| T8 | 每轮结束 | `Stop` | ledger | `CRITICAL` 且本轮没产出交接 → `decision:"block"` 逼它写完 |
| T9 | 压缩前 | `PreCompact` | 内存 todo | 快照进度进 gate，防压缩后失忆 |

### T5 是唯一允许「同步起 app-server」的地方

昂贵命令正则（配置项，默认）：

```
(pnpm|npm|yarn|bun)\s+(install|i|add|ci)\b
(pnpm|npm|yarn)\s+(run\s+)?(build|test|lint)\b
(pytest|vitest|jest|go test|cargo (build|test)|make)\b
git\s+(push|commit|rebase|merge|reset)\b
(docker|docker-compose|kubectl|terraform|helm)\b
(deploy|publish|release)\b
```

命中 → 强制刷新（绕 TTL）→
- `OK` / `TIGHT` → 放行
- `CRITICAL` → `permissionDecision: "deny"` + reason：先写交接，别启动长任务
- `EXHAUSTED` → deny

理由：长任务最容易被砍在中途，启动前是最后的决策点。

---

## 5. 暂停协议（agent 侧，写进 SKILL.md）

顺序固定，不可跳步：

1. 停手。不再派发工具调用，不开始新子任务，不回滚已完成的改动。
2. 采集现场：
   - `git status --porcelain`
   - `git diff --stat` + `git diff` 摘要
   - `git stash create`（只产出一个 commit 引用，**不动工作区**）→ 记 sha
   - 当前 todo 状态、最近失败命令的日志尾部（各 ≤ 20 行）
3. 写交接文档（§6）。
4. 输出固定 6 行暂停摘要：原因 / 剩余额度 / 交接路径 / 建议接手方式 / resume 命令。
5. **不许**：伪造完成、悄悄缩范围后交付、把未完成项写成「已实现，待验证」。

---

## 6. 交接文档

落盘位置（二选一，默认 A）：

- A `<repo>/.handoff/<utc>-<slug>/HANDOFF.md` + `handoff.json`
- B 接入现有 `codex-cooperation`：额外跑 `handoffctl create --source-path ...` 生成可验收任务单

### 6.1 `handoff.json`（机器读）

```json
{
  "id": "ho-20261111T0330Z-a1b2",
  "created_at": "2026-11-11T03:30:12Z",
  "status": "partial",
  "source": {"agent": "codex", "model": "gpt-5.6-sol", "session_id": "01a1...", "transcript": "/mnt/c/.../rollout-....jsonl"},
  "quota": {"p5": 96.0, "r5_at": 1794382158, "p7": 41.0, "verdict": "HANDOFF_NOW"},
  "repo": {"path": "/home/callmesoul/code/x", "branch": "feat/y", "head": "abc1234", "dirty": true, "stash": "def5678"},
  "goal": "...",
  "done": ["..."],
  "todo": [{"order": 1, "task": "...", "entry": "src/a.ts:42", "verify": "pnpm test a"}],
  "decisions": ["..."],
  "pitfalls": ["..."],
  "resume_prompt": "..."
}
```

### 6.2 `HANDOFF.md` 模板（人 / 下个 agent 读）

必备九段，缺一段算不合格：

1. **目标与验收标准** —— 什么样算做完，可执行。
2. **已完成** —— 带证据：commit / 文件行号 / 测试输出片段。
3. **未完成** —— 有序，每条含「入口文件:行 / 期望行为 / 验证命令」。
4. **关键决策与理由** —— 为什么不用另一个方案，防止接手方推翻重来。
5. **坑** —— 试过且失败的路及原因（省下接手方 30 分钟）。
6. **环境与复现** —— 依赖安装、跑测命令、需要的 secret 是否存在。
7. **下一步第一动作** —— 必须 5 分钟内能开始，不许写「继续开发」。
8. **待确认问题** —— 卡在用户决策上的点。
9. **Resume prompt** —— 可直接粘给下一个 agent 的整段话，自包含，不引用本会话。

### 6.3 接手侧入口

在全局 `AGENTS.md` 加一条：

> 会话开始时若仓库存在 `.handoff/`，取最新未完成的一份，读 `HANDOFF.md`，先复述「目标 + 下一步第一动作」再动手。

---

## 7. 文件树与职责

```
codex-quota-guard/
├── SKILL.md                    # 重写：隐式触发描述 + 判定/暂停/交接协议
├── PLAN.md                     # 本文件
├── agents/openai.yaml          # allow_implicit_invocation: true + policy
├── config/
│   ├── thresholds.yaml         # 阈值、reserve、N/K、昂贵命令正则
│   └── handoff-template.md
├── hooks/
│   └── hooks.json              # install 时拷到 $CODEX_HOME/hooks.json
├── scripts/
│   ├── quota_status.py   # 保留；加 --from-rollout 复用归一化
│   ├── quota_guard.py          # 新：hook 入口 + status/plan/check 子命令
│   └── handoff_writer.py       # 新：采现场 + 渲染交接
├── tests/fixtures/*.jsonl      # 假额度样本（含 EXHAUSTED / 过期 resets_at）
└── install.sh                  # symlink skill + 合并 hooks.json + 打印信任提示
```

### 7.1 `quota_guard.py` 子命令

```
quota_guard.py hook <event>        # 从 stdin 读 payload，写 stdout JSON
quota_guard.py status [--json]     # 人看：当前等级 + 剩余 + 建议
quota_guard.py plan --items 7      # 写入 est_turns 并立即判定
quota_guard.py check [--milestone] # agent 主动查
quota_guard.py selftest            # 用 fixtures 跑断言
```

统一退出码（hook 之外）：`0 OK`、`10 TIGHT`、`11 CRITICAL`、`12 EXHAUSTED`、`13 UNKNOWN`。
`13` 是 **fail-open**：拿不到额度就放行，只在 `hook.log` 记一笔，绝不让测量失败阻断干活。

### 7.2 关键代码骨架

尾部读 rollout（唯一有性能风险的地方）：

```python
def tail_rate_limits(path: str, window: int = 65536) -> dict | None:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - window))
        buf = f.read().decode("utf-8", "ignore")
    for line in reversed(buf.splitlines()):
        if '"rate_limits"' not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue           # 首行可能被切断，跳过
        rl = json.loads(line).get("payload", {}).get("rate_limits") or ...
        if rl:
            return rl
    return None
```

hook 分发（fail-open 兜底）：

```python
def hook(event: str) -> int:
    try:
        payload = json.load(sys.stdin)
        gate = refresh(payload, event)          # 内部按事件决定是否真去取数
        return emit(event, gate, payload)       # 输出 JSON / 退出码
    except Exception as e:
        log_error(e)
        return 0                                # 绝不因为护栏挂了挡住干活
```

Stop hook 逼它写交接（循环保护靠 `stop_hook_active`）：

```python
if verdict in ("CRITICAL", "EXHAUSTED") and not payload.get("stop_hook_active"):
    if not latest_handoff_is_fresh(payload["cwd"]):
        print(json.dumps({"decision": "block",
            "reason": "配额已进入 CRITICAL：结束前必须先写 .handoff/<id>/HANDOFF.md，"
                      "九段齐全，然后输出 6 行暂停摘要。写完后正常结束。"},
            ensure_ascii=False))
        return 0
```

### 7.3 `hooks.json`

```json
{
  "hooks": {
    "SessionStart": [{"matcher": "startup|resume|compact|fork", "hooks": [
      {"type": "command", "command": "python3 $HOME/.agents/skills/codex-quota-guard/scripts/quota_guard.py hook SessionStart",
       "timeout": 15, "statusMessage": "检查 Codex 配额"}]}],
    "UserPromptSubmit": [{"hooks": [
      {"type": "command", "command": "python3 $HOME/.agents/skills/codex-quota-guard/scripts/quota_guard.py hook UserPromptSubmit", "timeout": 6}]}],
    "PreToolUse": [{"matcher": "Bash", "hooks": [
      {"type": "command", "command": "python3 $HOME/.agents/skills/codex-quota-guard/scripts/quota_guard.py hook PreToolUse", "timeout": 8}]}],
    "PostToolUse": [{"matcher": "Bash", "hooks": [
      {"type": "command", "command": "python3 $HOME/.agents/skills/codex-quota-guard/scripts/quota_guard.py hook PostToolUse", "timeout": 5, "async": true}]}],
    "PostModelRequest": [{"hooks": [
      {"type": "command", "command": "python3 $HOME/.agents/skills/codex-quota-guard/scripts/quota_guard.py hook PostModelRequest", "timeout": 5, "async": true}]}],
    "PreCompact": [{"hooks": [
      {"type": "command", "command": "python3 $HOME/.agents/skills/codex-quota-guard/scripts/quota_guard.py hook PreCompact", "timeout": 5}]}],
    "Stop": [{"hooks": [
      {"type": "command", "command": "python3 $HOME/.agents/skills/codex-quota-guard/scripts/quota_guard.py hook Stop", "timeout": 10}]}]
  }
}
```

---

## 8. SKILL.md 重写要点

description 现在是「问用量时才用」，必须改成同时覆盖三条触发路径：

```
description: >
  Track the signed-in Codex account's quota windows (5-hour and weekly) and guard long tasks
  against running out of quota. Use at the start of any coding or multi-step task, after each
  completed milestone, before long-running or expensive commands, before final delivery, when a
  rate-limit or 429 error appears, or when asked about remaining usage, reset times, or handoffs.
```

注意别写成「任何对话都触发」—— 闲聊也注入 500 字护栏是纯浪费，且会让模型对护栏脱敏。**覆盖「任务会话」，不覆盖「聊天会话」**，这条边界靠 T0 的 `session` 判定 + T1 的 `EXHAUSTED` 才 block 来守。

正文结构固定为五块：判定口径 → 检查时机 → 暂停协议 → 交接 schema → 报错处理。
每块都要能独立执行，因为 skill 可能只被加载一次，中途被压缩驱逐。

`agents/openai.yaml`：

```yaml
interface:
  display_name: "Codex Quota Guard"
  short_description: "配额护栏 + 自动交接"
  default_prompt: "检查当前 Codex 配额，判断这个任务能否做完；做不完就先写交接文档。"
policy:
  allow_implicit_invocation: true
```

---

## 9. 部署与坑

1. `ln -s ~/code/codex-quota-guard ~/.agents/skills/codex-quota-guard`（Codex 支持 symlink）。
2. 合并 `hooks/hooks.json` 到 `$CODEX_HOME/hooks.json`（= `/mnt/c/Users/callmesoul/.codex/hooks.json`）。
3. **hook 信任**：非托管 hook 首次运行要 `/hooks` 审查并按 hash 信任；改了 hook 文件要重新信任。这是落地第一摩擦点，`install.sh` 结尾必须打印提示。自动化场景可临时 `--dangerously-bypass-hook-trust`。
4. `AGENTS.md`（`~/.codex/AGENTS.md`，现在是 0 字节空文件）写三行：

```markdown
## 配额护栏
每个任务会话开始时读 `~/.codex/quota-guard/gate.json`；`verdict != OK` 时按
@.agents/skills/codex-quota-guard/SKILL.md 的暂停协议执行，先写交接再停。
```

5. 状态目录 `$CODEX_HOME/quota-guard/`：`gate.json` / `ledger.jsonl` / `hook.log`。
   注意它在 `/mnt/c`（DrvFs），小文件写延迟 ~1-5ms，可接受；但**不要**在 hook 里做 fsync 循环。
6. Windows 侧若也要跑，hooks.json 里每条加 `commandWindows`（`python` + `C:\Users\...` 路径）。

---

## 10. 验证

| 用例 | 手段 | 期望 |
|---|---|---|
| 额度正常 | fixture `ok.jsonl` | T0 注入 OK 行；`status` 退出 0 |
| 接近耗尽 | fixture `critical.jsonl` | Stop 输出 `decision:block`；agent 写交接 |
| 已耗尽 | `QUOTA_GUARD_FORCE=EXHAUSTED` | PreToolUse 对任何 Bash 返回 deny（exit 2） |
| resets_at 过期 | fixture `stale.jsonl` | 视为 0，并回落到 app-server |
| 取数失败 | 把 codex 从 PATH 摘掉 | exit 0 放行 + `hook.log` 记 error（fail-open） |
| 端到端 | `codex --dangerously-bypass-hook-trust "改个 todo 注释"` | `hook.log` 出现 SessionStart/PostToolUse/Stop 三条 |

`selftest` 要能在 CI 里跑，不依赖真额度。

---

## 11. 风险与边界

| 风险 | 影响 | 对策 |
|---|---|---|
| hook 只 match Bash，`apply_patch` 编辑不计入 | T3 计数漏 | 靠 T2/T6 agent 主动检查补 |
| 模型先把话说满再结束 | 绕过 T8 | `Stop` block + `stop_hook_active` 循环保护 |
| hook 输出上限 ~2500 token | 注入过多被截断 | 护栏正文 ≤ 40 行，详细内容放文件让它读 |
| DrvFs 上并发写 gate.json | 写坏 | 写临时文件 + `os.replace` 原子换 |
| burn 估计不准（前期样本少） | 误判 CRITICAL | 前 5 轮用 default 1.0；只做「要不要警告」，硬停只认 429 与 `r5 ≤ 0.5` |
| 反复警告导致脱敏 | 护栏失效 | 同一等级同一窗口只注入一次（`notified_at` 去重） |
| `/mnt/c` 与 WSL 双环境路径 | 找不到文件 | 所有路径从 `CODEX_HOME` 派生，禁止硬编码 |

---

## 12. 分期

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | rollout 尾部解析 + `status` 命令，不动 hooks | `status` 与 `/status` 数值一致 |
| P1 | hooks 骨架（T0/T1/T3/T8）+ gate.json | 端到端日志四条齐全 |
| P2 | 判定与估算（burn、plan、T5 昂贵命令） | 假数据能复现 CRITICAL deny |
| P3 | `handoff_writer.py` + 模板 + 接手侧入口 | 新会话只读交接文档能接上 |
| P4 | 去重、fail-open、selftest、install.sh | selftest 全绿 |
| P5 | 接 `codex-cooperation`（可选） | 交接能转成 handoffctl 任务单 |

P0 + P1 就能解决 80% 的问题：**至少不会在没额度时一头撞死**。

---

## 13. 已定的四件事（2026-11-11）

1. 阈值：`CRITICAL = 5h 剩余 10% / 周剩余 5%`，`TIGHT = 15% / 10%`，`EXHAUSTED = 0.5%`。2026-11-12 根据真实耗尽事故把 5h 阈值从 5% 提前到 10%；超长上下文单次请求会消耗数个百分点，5% 不足以可靠生成并填写交接。
2. `WAIT_RESET`：**默认关闭**，逻辑保留、`config/thresholds.yaml` 里一个布尔开关。
   关闭的理由：5% 阈值下再等重置，收益不如直接交接稳定；开着等于把会话挂住。
3. 交接落点：`<repo>/.handoff/<utc>-<slug>/`，用 `.git/info/exclude` 本地忽略，不进版本库。
4. Windows 侧先不装；桌面版的 home（`/mnt/c/Users/callmesoul/.codex`）需要时加 `--also`。

### 2026-11-12 事故修正

- `PostModelRequest` 只保留诊断/计数职责；其输出无法阻止请求或改变模型上下文。
- `PreToolUse` 扩展到所有受支持的本地工具。进入 CRITICAL 后自动生成草稿，随后只放行现场采集、`quota_guard.py handoff` 和对草稿 `HANDOFF.md` 的编辑。
- `Stop` 不再把仅含 TODO 的新鲜草稿误判为完整交接，并按当前 session 选择交接文件。

## 14. 落地记录

已实现 P0–P4，可运行：

| 文件 | 作用 |
|---|---|
| `scripts/quota_guard.py` | hook 入口 + `status/plan/check/handoff/selftest` |
| `scripts/quota_status.py` | 原脚本保留，作为 app-server 兜底 |
| `config/thresholds.yaml` | 阈值、节奏、昂贵命令正则、放行正则 |
| `hooks/hooks.json` | 7 个事件的模板，`install.sh` 渲染绝对路径后合并 |
| `install.sh` | 建软链 + 合并 hooks + 写 AGENTS.md + 卸载 |
| `SKILL.md` | 判定口径 / 检查时机 / 暂停协议 / 交接 schema |

验证结果：

- `quota_guard.py selftest` 全绿（判定分支、rollout 尾部解析、截断行、gate 原子写、交接生成、git 排除）。
- `QUOTA_GUARD_FORCE=EXHAUSTED` → `PreToolUse` 对 `pnpm build` 返回 `permissionDecision: deny`；
  对 `quota_guard.py handoff` 与 `git status` 放行（写交接必须能用）。
- `QUOTA_GUARD_FORCE=CRITICAL` → `Stop` 在无交接时返回 `decision: block`；有新鲜交接时只给 `systemMessage`。
- 真实数据：`status` 走 rollout 源，5h 剩 76%，与 `/status` 一致；不启动 app-server。

剩余人工步骤：在 Codex 里跑 `/hooks` 信任这些条目（或临时 `--dangerously-bypass-hook-trust`）。

---

## 15. 插件化：handoff-board（2026-11-12）

目标：把「项目 → 会话 → 交接文档」做成可安装的 Codex 插件面板（对应需求 m00002）。

### 15.1 为什么是这个形态

Codex 0.154.0 的插件只装载 skills / MCP / apps / agents / commands / hooks，**没有自定义 UI 扩展点**
（`features.enable_mcp_apps` 仍是 under development 且默认 false）。宿主唯一原生的「面板」通道是
visualize 插件的内联可视化内容引用：

```
\ue200visualize\ue202{"path":"<absolute>.html","title":"..."}\ue201
```

被引用的文件必须是 HTML **片段**（无 doctype/html/head/body）、<1MB、不发 fetch/XHR/WebSocket。
Windows 桌面版二进制里 `\ue200visualize\ue202` 是编译进常量表的明文，证书层面第三方插件可用。

因此面板做成**一份数据 + 两种呈现**：内联可视化（对话里）与本地 HTTP（浏览器 / 远程）。

### 15.2 文件

| 文件 | 作用 |
|---|---|
| `.agents/plugins/marketplace.json` | 本地 marketplace，暴露 `./plugin` |
| `plugin/.codex-plugin/plugin.json` | 插件清单，`skills: "./skills/"` |
| `plugin/scripts/handoff_board.py` | `scan / render / serve / marker`，仅 stdlib |
| `plugin/assets/panel.fragment.html` | 构建产物：Vue 面板的单文件 HTML 片段，`id="hb-data"` 占位 |
| `plugin/server.py` | 本地 MCP App 服务器（`ui://handoff-board/index.html` + `open_handoff_board`） |
| `plugin/skills/handoff-board/SKILL.md` | 技能说明：定位脚本、发标记、口径 |
| `panel/handoff-board-panel/` | Vue 3 + shadcn-vue 源码（不在插件缓存里，`node_modules` 不打包） |

数据连接：`~/.codex/sessions/*/*/*/rollout-*.jsonl` 的 `payload.cwd` + `session_index.jsonl` 的
`thread_name` 组成项目/会话层；`<project>/.handoff/*/handoff.json` + `HANDOFF.md` 组成交接层。
交接的 `session_id` 在本地没有 rollout 时，仍以 `handoff only` 行显示（现存那份就属于这种情况）。

### 15.3 安装与刷新

`install.sh` 现在四步变五步，第 4 步自动装插件：

```bash
./install.sh            # 技能 + hooks + 插件 + AGENTS.md
./install.sh --no-plugin
./install.sh --uninstall  # 同时摘插件与 marketplace
```

插件缓存是**拷贝不是软链**，改任何插件文件后必须重装：

```bash
codex plugin remove handoff-board@codex-quota-guard
codex plugin add handoff-board@codex-quota-guard
```

### 15.4 技能重复注册的修法

`~/.agents/skills/codex-quota-guard` 原先整棵软链到项目根，把 `plugin/skills/handoff-board`
也带进了技能扫描，导致 `codex debug prompt-input` 里同名技能出现两次（r1 与 r3 各一条）。
现在改为**逐项软链**（SKILL.md / PLAN.md / scripts / config / hooks / agents），不再暴露 `plugin/`；
`install.sh` 的第 1 步已按此实现，并拒绝覆盖非本项目的目录。

### 15.5 验证

- `scan` → 17 projects / 656 sessions / 1 handoff。
- `marker` → 输出单个含 U+E200/E202/E201 的行，指向 `~/.codex/visualizations/handoff-board/...html`。
- `serve` → `/` 200 / `/healthz` ok / `/api/board` JSON；每次请求重建，跟磁盘。
- 片段约束检查：无 doctype/html/head/body，无 fetch/XHR/WebSocket，269 KB。
- 视觉检查（chromium headless）：1100 / 736 / 420px，浅色与深色，三列与堆叠布局均无溢出。

### 15.6 面板重写为 Vue 3 + shadcn-vue（2026-11-12）

原始面板是 518 行原生 HTML/CSS/JS 单文件。改成 Vue 3 + shadcn-vue（Tailwind v4）后，源码在
`panel/handoff-board-panel/`，构建产物仍是**同一个单文件片段**，所以内联可视化与 `serve` 两种呈现
共用一份模板，Python 侧只换模板路径与注入点。

**构建管线（`panel/handoff-board-panel/vite.config.ts`）**

- library 模式（`build.lib.entry = src/main.ts`、`formats: ['iife']`、`name: 'HandoffBoard'`）——
  不走 `index.html`，否则 `generateBundle` 会把整份 index.html（含 doctype）当资源拼进产物。
- 自定义 `inlineFragment()` 插件在 `generateBundle` 里把 CSS 资产与入口 chunk 拼成片段：
  `<!-- Board payload -->` + `<script type="application/json" id="hb-data">null</script>` +
  `<div id="hb-root"></div>` + `<style>…</style>` + `<script type="module">…</script>`，
  并对 `</script` 做转义，`emitFile('panel.fragment.html')`。
- `define: {'process.env.NODE_ENV': '"production"'}` 是必需的：宿主 iframe 没有 Node 垫片，
  否则 Vue/shadcn 运行时探测 `process.env.NODE_ENV` 直接抛 `ReferenceError`，页面全白。
- `build.target: 'es2022'`、`cssCodeSplit: false`、`assetsInlineLimit: 1MB`、`modulePreload: false`。

**样式契约（关键坑）**

- 片段与宿主 kit 共享同一个 document，所以**不能用 `@import "tailwindcss"`**（preflight 的
  `*{margin:0;padding:0}` 会改掉宿主自己的元素）。改为 `@import "tailwindcss/theme.css" layer(theme)`
  + `@import "tailwindcss/utilities.css" layer(utilities)`，再自己写作用域到 `#hb-root` 的最小重置。
- **片段里不要定义 `:root` 色板**：宿主 `visualize.css` 已经提供 `--background/--foreground/--card/…`
  并用 `:root[data-theme="dark"]` 切换；片段自带 `:root` 会盖掉宿主配色。独立页面由
  `STANDALONE_TOKENS` 定义**同名**变量 + `prefers-color-scheme`，两边一套名字。
- **不要照抄宿主 CSS 里的 `light-dark()`**：Vite 的 lightningcss 会把它降级成
  `var(--lightningcss-light,#fff)var(--lightningcss-dark,#222)`，而这两个变量在产物里从未定义，
  于是 `--secondary` 计算成字面串 `#fffffff5#363636f5`，Badge/Checkbox 全部透明无边框。
  修法就是上一条：不写 `light-dark()`，只用 `var(--token, 兜底值)`。
- `@theme inline` 里保留 `--breakpoint-panel: 820px` / `--breakpoint-narrow: 560px`，供
  `max-panel:` / `max-narrow:` 变体使用（Tailwind v4 的 `max-*` 变体来自自定义断点）。
- 文档区跨栏用 CSS `.hb-doc { grid-column: 1 / -1 }` + `@media not all and (min-width: 820px)`，
  **不能用 `col-span-2`**：单列 grid 下它会造出隐式第二列，420px 时第一列被压成 0px。

**组件结构**

`src/App.vue`（头部 stats、`Input` 过滤框、`Checkbox`、三列 grid）+ `src/components/board/`
`ProjectList.vue`（`Button` + `Badge` + `ScrollArea`）、`SessionList.vue`（含 `handoff only`
孤儿行）、`HandoffDoc.vue`（`Empty` 三档空态、`Badge` 元信息、`Separator`、`v-html` 渲染
`HANDOFF.md`、Ask Codex 按钮）。状态在 `src/lib/board.ts` 的 `useBoard()`，Markdown 渲染器在
`src/lib/markdown.ts`（与原实现逐条对齐：围栏/标题/表格/列表/引用/分隔线/行内 code+粗体+链接），
数据入口在 `src/lib/data.ts`（先读 `#hb-data`，回落 `window.__HANDOFF_BOARD__`）。

**Python 侧改动**

- `TEMPLATE = plugin/assets/panel.fragment.html`；`DATA_SLOT` 是
  `<script type="application/json" id="hb-data">null</script>`，`render_fragment()` 断言占位存在后
  替换成真实 JSON（`</` 转义）。
- `STANDALONE_TOKENS` 改写为定义与宿主同名的 `--background`…`--ring` + `--radius`，删掉旧原生控件样式。

**刷新流程**

```bash
cd panel/handoff-board-panel && npx vue-tsc -b && npx vite build
cp dist/panel.fragment.html ../../plugin/assets/panel.fragment.html
cd ../.. && codex plugin remove handoff-board@codex-quota-guard && codex plugin add handoff-board@codex-quota-guard
```

**验证**

- `vue-tsc -b` rc 0；`vite build` → 202 KB 片段（gzip 65.7 KB），`--lightningcss` 计数 0，纯度检查 clean。
- 真实数据 `render_fragment` 439 KB / `render_standalone` 441 KB；`serve` `/` 200、`/healthz` 200、
  `/api/board` 200（260 KB JSON）。
- 用官方 `visualize/1.0.21/skills/visualize/scripts/render.py` 走真实双层 iframe 管线截图：
  1100px 三列、736px 两列 + 文档通栏、420px 单列；深浅色跟随 `data-theme`。
  截图归档在 `docs/panel-736.png`、`docs/panel-420.png`、`docs/panel-1100-dark.png`。

### 15.7 桌面版侧栏「Handoff Board」空白：五个连环坑（2026-11-12）

症状：侧栏能看到 Handoff Board 按钮，点开后主区域一直转圈 / 一片空白。

**坑 1：桌面版跑的是 Windows 侧缓存，不是 WSL 侧。** 之前一直以为 App 通过 `wsl.exe` 启动 WSL
里的 codex、所以用 WSL 的 `~/.codex`。实际用 `/proc/*/cmdline` 查宿主拉起的 server 进程，
cwd 是 `/mnt/c/Users/callmesoul/.codex/plugins/cache/...`、`CODEX_HOME=/mnt/c/Users/callmesoul/.codex`。
两个 home 各自独立，`codex plugin add` 只装到执行它的那个；**装完必须确认 Windows 侧缓存里也有新版本**。

**坑 2：`command` 必须是 `python3`。** 桌面 App 拉起 MCP server 用的是非登录 shell 的窄 PATH，
WSL 里 `python` 不存在（只有 `/usr/bin/python3`）。写 `"python"` 时宿主日志报
`MCP client for handoff_board failed to start: MCP startup failed: No such file or directory (os error 2)`。
用 `env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin bash -c 'command -v python3'`
验证，别用 `wsl.exe -d Ubuntu -- bash -lc`（那是登录 shell，PATH 更宽，会误判）。

**坑 3：stdout 必须按字节写 UTF-8。** `sys.stdout.write` 走的是继承的控制台代码页，
大 payload（含 `cn@0.5.2` 打包的 `\u014f`）会抛 `UnicodeEncodeError: 'gbk' codec can't encode character`，
`resources/read` 直接返回 `-32603`，面板空白。`server.py` 用
`sys.stdout.buffer.write((payload + "\n").encode("utf-8"))` + flush，并在 `main()` 开头
`harden_streams()`（`reconfigure(encoding="utf-8", errors="replace")`）兜底。

**坑 4：`text/html;profile=mcp-app` 要求应用主动握手。** 宿主对两种 UI mime 的处理不同：
`requiresMcpInitialization = ("text/html;profile=mcp-app" === mimeType)`。选这个 mime 就必须在 iframe
里发 `ui/initialize` 再发 `ui/notifications/initialized`，否则 5 秒后报
「应用未能在 5 秒内完成初始化」。我们的面板是纯静态页，没有 bridge，所以改用
**`text/html+skybridge`**（宿主同样接受，但不触发握手）。`server.py` 里是 `WIDGET_MIME` 常量。

**坑 5：`enable_mcp_apps` 要显式打开。** 0.167 里它仍是 under development 且默认 false，
两个 home 的 `config.toml` 都要加：

```toml
[features]
enable_mcp_apps = true
```

渲染门控在 asar 里是 `(0,el.Ou)("2138468235").get("enable_mcp_apps",!1) && (0,nl.wH)()`，
其中 `wH` 恒返回 true（远端 kill-switch），所以本地只需管前半段。

**怎么确认真的渲染了。** 桌面日志不在 `~/.codex/logs_2.sqlite`，在
`%LOCALAPPDATA%\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\Codex\Logs\<Y>\<M>\<D>\codex-desktop-<uuid>-<pid>-t0-i1-<hhmmss>-0.log`。
成功的一轮会依次出现：`IAB_LIFECYCLE ownerRoutePath=/mcp-app/handoff_board/open_handoff_board` →
`Found app view` → `Calling app tool` → `Reading app resource uri=ui://handoff-board/index.html` →
`mcp_app_sandbox.session_created displayMode=fullscreen` → `Starting app sandbox` → `guest_attached` →
`sandbox_document_committed` → `Connected app sandbox` → `Starting app widget` → `widget_frame_committed` →
**`Started app widget`**。同时 `server.py` 的 `debug_log()` 会在 `/tmp/handoff-board-mcp.log`
记下 `recv id=… method=resources/read uri=…`（可用 `HANDOFF_BOARD_MCP_LOG` 改路径）。
只要没有 `MCP app error` 与 `level=error`，剩下的就只是面板自身的样式/布局问题。

**验证路径（不依赖 GUI）。** `render_standalone()` 产出的 HTML 可以直接用
`chromium --headless=new --screenshot` 截图，面板本身没问题时一定是完整三栏；
所以「HTML 对不对」和「宿主给不给显示」能分开定位。

**版本演进**：0.3.1（误改 `python`）→ 0.3.2（stdout 字节写）→ 0.3.3（`harden_streams`）→
0.3.4（回退 `python3` + skybridge mime）→ 0.3.5（`debug_log`，并把 Windows 侧缓存也装到 0.3.5）。

**通用教训**：`codex plugin list` 会挂起（>120s），别在脚本里等；`codex mcp list` 正常。
`codex plugin remove` 必须写全名 `handoff-board@codex-quota-guard`，只写短名会报
`plugin requires --marketplace unless passed as <plugin>@<marketplace>`。
对 Windows 挂载路径做全树 grep / `Get-ChildItem -Recurse` 会卡死，挖 `app.asar` 用 mmap 定位。

**结果（2026-11-12 16:00）**：五个坑都修完后，侧栏点开即完整渲染三栏
（17 projects / 656 sessions / 4 handoffs），桌面日志出现 `Started app widget`、无任何 `MCP app error`。
0.3.5 同时装进了 WSL 与 Windows 两个 home 的缓存，八项文件 sha256 与源一致。

`docs/panel-{1100,736,420,1100-dark}.png` 用当前构建重新截了一遍（直接对 `render --standalone`
的产物跑 chromium headless，不经过宿主），用来区分「HTML 本身坏了」与「宿主不给显示」——
这一步在排查时反复用到：页面在 chromium 里正常、宿主却空白，问题就一定在宿主侧（mime / 握手 / flag / 缓存 home）。


---

## 16. 改名 codex-usage-status → codex-quota-guard（2026-11-12）

### 16.1 为什么改

`codex-usage-status` 名不副实：它听起来像 `/status` 的只读封装，而项目 90% 的价值在
「额度快没了就 deny 工具调用 + 逼写交接」这条护栏；而且它只覆盖三层里的 L1 取数，
不含交接协议与 handoff-board 面板。更关键的是**代码内部早就在用 `quota-guard` 这个名字**
（`AGENTS.md` 的 `codex-quota-guard:begin/end` 标记、交接模板署名、状态目录 `~/.codex/quota-guard/`），
所以改名不是引入第四种叫法，而是消除已有不一致。

否决的候选：`codex-handoff`（丢掉查额度入口，且与插件名 `handoff-board` 语义重复）、
`codex-baton`（隐喻，不看 README 猜不到）、`codex-quota-handoff`（太长，
`handoff-board@codex-quota-handoff` 里 handoff 出现两次）、加 `-doc`（`-doc` 习惯上表示
「关于该工具的文档」而非「该工具产出文档」，会被误认为文档站）。

### 16.2 改了哪些

| 位置 | 旧 | 新 |
|---|---|---|
| 目录 | `~/code/codex-usage-status` | `~/code/codex-quota-guard` |
| 技能目录 | `~/.agents/skills/codex-usage-status/` | `~/.agents/skills/codex-quota-guard/` |
| 兜底脚本 | `scripts/codex_usage_status.py` | `scripts/quota_status.py` |
| 技能名 | `SKILL.md: name: codex-usage-status` | `name: codex-quota-guard` |
| MCP client | `codex_usage_status_skill` / `Codex Usage Status Skill` | `codex_quota_guard_skill` / `Codex Quota Guard Skill` |
| marketplace id | `[marketplaces.codex-usage-status]` | `[marketplaces.codex-quota-guard]` |
| plugin id | `handoff-board@codex-usage-status` | `handoff-board@codex-quota-guard` |
| 插件缓存 | `~/.codex/plugins/cache/codex-usage-status/` | `~/.codex/plugins/cache/codex-quota-guard/` |

`install.sh` 因为 `DEST`、`codex plugin ...` 全是从名字拼出来的，改完直接重跑即可；
两个 home 都跑了 `./install.sh --also /mnt/c/Users/callmesoul/.codex`。

### 16.3 迁移时的顺序坑

1. **不能先动目录**：shell 的 cwd 就在项目里，`mv` 掉之后相对路径全失效。
   先把所有文件内容改完、验证 `grep` 干净，最后才 `mv` 目录，然后 `cd ..` 再操作。
2. **软链会随旧目录一起悬空**：`~/.agents/skills/codex-usage-status/` 是逐项软链，
   `mv` 之后每个链接都指向不存在的路径。删掉旧技能目录、重跑 `install.sh` 让它按新名重建。
3. **插件缓存是拷贝，旧目录不会自己消失**：`~/.codex/plugins/cache/codex-usage-status/`
   和 Windows 侧那份都要手动 `rm -rf`，否则 `codex plugin list` 里会同时出现两个 marketplace。
4. **`config.toml` 与 `hooks.json` 里是写死的绝对路径**：`sed -i 's|codex-usage-status|codex-quota-guard|g'`
   一次改掉两个 home 的这两个文件，比让 `install.sh` 去猜更省事（`install.sh` 会按 `quota_guard`
   关键字清理旧 hook 条目，但不会重命名已有的 marketplace 段）。
5. **hook 信任按文件 hash 记**：`hooks.json` 里 7 条命令的路径变了，hash 全变，
   **必须重新 `/hooks` 信任**（或 `--dangerously-bypass-hook-trust`），否则 hook 静默不执行。
6. **`.codex-global-state.json` 里残留旧路径**（项目列表、thread 描述、`resourceRevision`）。
   这是桌面版的 UI 状态缓存，`grep` 会命中，但不影响功能；改它风险大于收益，留着。

### 16.4 验证

- `grep -rn 'codex-usage-status\|codex_usage_status'` 在项目内只剩 `.workbuddy/memory/` 的历史记录（有意保留）。
- `python3 scripts/quota_guard.py selftest` → `SELFTEST OK`。
- `hook SessionStart` 返回正常 `additionalContext`；`status --refresh` 走 rollout 源、退出码符合判定。
- `codex plugin list` → `handoff-board@codex-quota-guard  installed, enabled  0.3.5`。
- `codex mcp list` → `handoff_board` 的 cwd 已是 `.../cache/codex-quota-guard/handoff-board/0.3.5/.`。
- 两个 home 的插件缓存与源 `diff -rq` 只差 `__pycache__`。

---

## 17. hook 信任自动化 + PostModelRequest 的版本依赖（2026-11-12）

### 17.1 问题：改名后护栏静默失效

Codex 对**非托管 hook** 按「内容 hash」记信任，落在 `config.toml` 的
`[hooks.state."<hooks.json 绝对路径>:<snake_event>:<group>:<handler>"] trusted_hash`。
`hooks.json` 一改（改名、调 timeout、换路径、改 matcher），旧 hash 全部失效，
而**未信任的 hook 是完全不执行的**——不报错、不警告。护栏看起来装着，实际一条没跑。
改名那次 7 条命令的路径全变，两个 home 的护栏同时静默死掉。

### 17.2 关键发现：`trusted_hash` 就是 `hooks/list` 的 `currentHash`

- 值形如 `sha256:<64hex>`，由 codex 自己算；**无法从 handler 字段逆推**。
  试过且全部对不上：裸 command、`<path>:<event>:0:0` key、path+command、
  `json.dumps` 的 handler/group/group-array（`sort_keys` × `separators` × `indent` × 尾换行）、
  snake_case 字段名、`null` 填充、字段全子集（2^5）、Rust `Debug` 串、`sha256:` 前缀、
  各种分隔符拼接（`| : \n \0 \t 空格 逗号`）、二次 hash。
- 差分实验确认 hash 覆盖**全部** handler 字段：改 `timeout` / `statusMessage` / `async` /
  `additionalContextLimit` / `matcher` / `command` / 事件名 都会变；改顶层 `description`、
  或在同 group 里**追加第二个 handler** 则**不变**（说明按 `(event, groupIndex, handlerIndex)`
  逐条算，不是整文件 hash）。
- 所以唯一正路：起 app-server 问 `hooks/list` 拿 `currentHash`，原样写进 `trusted_hash`。
  没有 `hooks/commit` 之类的写入 RPC（163 个方法里只有 `hooks/list`），手写 config 是唯一途径。
- 顺带确认：**两处 `hooks.json` 的 `currentHash` 与文件路径、与 `description` 位置无关**，
  只依赖 handler 内容。所以 WSL 与 Windows 两个 home 的 6 条同名 hook hash 完全相同。

### 17.3 `scripts/hook_trust.py`（新增，290 行）

```
python3 scripts/hook_trust.py                                   # 只看
python3 scripts/hook_trust.py --apply                           # 写默认 home
python3 scripts/hook_trust.py --apply --home /mnt/c/Users/x/.codex
python3 scripts/hook_trust.py --apply --home ... --codex /path/codex.exe
```

- `home_is_windows()` / `to_windows_path()`：识别 `/mnt/c/...` 与 `C:\...` 两种形态，后者走 `wslpath -w`。
- `find_codex()`：**按 home 形态选可执行文件**（见 17.4，这是本脚本最容易踩的地方）。
- `list_hooks()`：起 app-server，`initialize` → `initialized` → `hooks/list`，只收
  `"quota_guard" in command` 的条目，返回 `{key: {hash, trust}}` 以及「不属于该 home 的 key」。
- `toml_key()`：key 里有反斜杠时用 **TOML 字面量字符串（单引号）**，否则 `C:\Users` 的 `\U`
  会被当转义序列，报 `too few unicode value digits`，整份 config.toml 判为非法、
  **静默回落到默认配置**（后果是信任全丢，比不写更糟）。这是实测踩到的。
- `rewrite_state()`：按行删掉旧 `[hooks.state]` / `[hooks.state."` / `[hooks.state.'` 块再追加，
  其它配置原样保留；备份名 `config.toml.bak-hooktrust`。
- 写后自动复查；条目数变少、仍有 untrusted、或复查拿不到条目都返回非 0。
- 已接进 `install.sh` 的第 4 步（`--no-trust` 可跳过），失败只警告不中断安装。

### 17.4 跨 home 的两个坑（对称的）

| home 形态 | 必须用的 codex | 用错的后果 |
|---|---|---|
| `/home/...`（WSL） | Linux 版（`~/.local/bin/codex`） | Windows 版 `codex.exe` 认不出 `/home/...`，**静默**回落到 `C:\Users\...`，打印出**另一个 home** 的答案，看起来却像本 home 的 |
| `/mnt/c/...`（Windows） | Windows 版 `codex.exe` | Linux 版 codex 在 sqlite 初始化就失败：`failed to initialize sqlite state runtime under /mnt/c/...`（sqlite 跨文件系统不兼容） |

第二个坑的绕法：**WSL 能直接执行 .exe**，所以用 Windows 版 codex.exe 起 app-server，
把 `CODEX_HOME` 设成 `C:\Users\...` 形式即可，`hooks/list` 完全可用。
第一个坑只能靠「按 home 形态选 exe」+「核对返回 key 的前缀是否属于该 home」双重保险，
脚本两样都做了。

另一个小坑：Python 读 `codex.exe` 的 stdout 必须显式 `encoding="utf-8", errors="replace"`，
否则按 GBK 解码会炸 `UnicodeDecodeError: 'gbk' codec can't decode byte 0xaf in position 977`。

### 17.5 `PostModelRequest` 是**桌面版/扩展**才有的事件（2026-11-13 更正）

先记原来的结论，再记实测推翻它的过程——因为原结论错在「只看了一个二进制」。

**原来的结论（错的）**：WSL CLI 是 `codex-cli 0.154.0`，二进制里 `PostModelRequest` 出现 0 次，
临时 home 装 7 个事件各一条 echo hook 跑 `codex exec` 只触发 5 个事件，所以断定
「0.154.0 不支持，`counters.model_requests` 会停在 943」。

**实测推翻（2026-11-13）**：

- WSL 里其实有**三个** codex 二进制，各自版本不同：

  | 二进制 | 版本 | `PostModelRequest` 出现次数 |
  | --- | --- | --- |
  | `~/.local/bin/codex` → `~/.codex/packages/standalone/releases/0.154.0-*/bin/codex` | `0.154.0` | **0** |
  | `/mnt/c/Users/callmesoul/.codex/bin/wsl/dda21de2dbbd1cc1/codex`（桌面版下发的 WSL 桥） | `0.167.0-alpha.4.1` | 1 |
  | `~/.vscode-server/extensions/openai.chatgpt-*/bin/linux-x86_64/codex`（VSCode 扩展） | `0.169.0-alpha.11.1` | 1 |

- 隔离实验（各自独立的 `CODEX_HOME`，7 事件各一条 echo hook，`--dangerously-bypass-hook-trust`）：
  - standalone `0.154.0` → `SessionStart` / `UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `Stop`，**无 PMR**；
  - 桌面桥 `0.167.0-alpha.4.1` → 同样 5 个 **+ `PostModelRequest` × 2**。
- 关键事实：桌面版和 VSCode 扩展都**用 `CODEX_HOME=/home/callmesoul/.codex`**（就是 WSL 的 home），
  所以它们的 hook 触发和 standalone CLI 写进**同一个** `~/.codex/quota-guard/hook.log`。
  `hook.log` 里那些 PMR 行来自桌面版/扩展，不是 standalone。
- `gate.json` 的 `counters.model_requests` 也一直在涨（实测 1185 → 1187），**从来没有停住**。
- 之前的错误来源：拿 `codex exec`（standalone）做的实验去推整个 home 的行为，
  而实际驱动这个 home 的主客户端是桌面版。

**结论（修正版）**：保留这条 hook 条目、不做版本分支——理由和原来一样（条目零成本、
新版会正常触发、护栏不依赖它），但**「0.154.0 上计数会停住」这个说法要删掉**：
只要桌面版或 VSCode 扩展在用这个 home，PMR 就会正常触发，计数正常增长。
只有「纯 standalone CLI、没有桌面版/扩展同时运行」这种场景才收不到 PMR，而那时护栏
由 `PreToolUse`（每 10 次 Bash，TIGHT 时每次都刷）和 `Stop` 兜底，不受影响。

### 17.6 验证结果

- 两个 home 的 `[hooks.state]`：WSL 6 条、Windows 7 条，`tomllib` 解析通过，全部 `trusted`。
- 实机（无 `--dangerously-bypass-hook-trust`）：`codex exec ... "reply with the single word: ok"`
  → `hook.log` 新增 `SessionStart` / `UserPromptSubmit` / `Stop` 三条，`verdict=OK`。
- `install.sh` 端到端跑通（6 步），插件在两个 home 都重装成功；
  顺手修了一个老问题：非登录 shell 的 PATH 没有 `~/.local/bin`，导致插件那步被静默跳过，
  现在 `install.sh` 自己把 `$HOME/.local/bin` 补进 PATH。

---

## 18. 换掉菜单图标（2026-11-13）

### 18.1 问题：旧图标在 20px 下读不出「交接」

原 `plugin/assets/handoff-board.svg` 是「一个圆角矩形 + 三根竖线 + 三个错落的实心圆点」，
想表达「看板的三列，每列有卡片」。实际渲染在侧栏的 16–20px 上：

- 三根竖线只隔 5 个单位（`M11/M16/M21`），在 24 网格里太密，20px 下糊成一片灰；
- 三个圆点（r=2）比线还小，缩到 20px 只剩 1.6px，看不出是点，只让线条更脏；
- 结果是最需要被认出的「图标形状」消失了，只剩一个带竖纹的方框——和「文档」「表格」「设置」
  这类图标撞脸，用户扫侧栏时认不出这是「交接板」。

### 18.2 换成什么：board + 出站箭头

新图标（24 网格，与面板已在用的 lucide 同规格：`stroke-width=2`、`stroke-linecap=round`）：

```svg
<rect x="2.75" y="4.75" width="12.5" height="14.5" rx="2.5"/>
<path d="M6.25 9.4h6M6.25 12.4h4"/>
<path d="M15.5 12h5.6"/>
<path d="M18.7 8.9 21.8 12 18.7 15.1"/>
```

语义拆解：
- **左边方框 + 两条横线** = 板子/清单（「board」这一半，也是「一堆交接文档」的形状）；
- **右边箭头穿出边框** = 工作被交出去（「handoff」这一半，即 `M15.5 12h5.6` 的箭杆
  从方框右边缘外 0.25 处起笔，视觉上像是从板子里递出来）。

这样「board」和「handoff」两个词各占一半视觉重量，不必看文字也能猜出功能。

### 18.3 为什么是这个方案（而不是别的）

在 24 网格上画了 10 轮候选，每轮都按「真机像素」验：用 cairosvg 渲成 16/20/24px，
再按 2× DPR 渲染后 LANCZOS 降采样（模拟 retina 屏的真实观感），最后放大 8–12 倍
用 NEAREST 看像素真值。被淘汰的方案与原因：

| 方案 | 淘汰原因 |
| --- | --- |
| 保留原三竖线，只把线加粗 | 加粗后 20px 下三列粘连成一团黑，比原来更糟 |
| 双卡片 + 中间箭头（`[→]` 形状） | 20px 下退化成 `[+]`，像「新增」按钮 |
| 纯文档 + 右下斜箭头 | 箭头和文档底边抢空间，16px 下箭头糊掉 |
| 收件盘 / 托盘（inbox） | 是「收」不是「交」，语义反了；且与邮件类图标撞脸 |
| 剪贴板 + 对勾 | 表达「已完成」，丢掉了「交给下一个会话」这层 |
| 三列看板 + 箭头（`▯▯▯→`） | 三列在 16px 下必然粘连，除非减到两列——减完就不像看板了 |
| 实心竖条 + 箭头（`▮▮→`） | 20px 下最清楚，但实心块在深浅色主题里重量失衡，且丢了「板子」的轮廓 |

**关键取舍：文字行数从 2 条降到 2 条但缩短**（`h6` / `h4`，而不是铺满板宽）。
铺满会让方框内部变成「实心块」，20px 下和 `▤` 撞脸；留白后轮廓仍然是个「框」。
箭杆长度 5.6、箭头张角 ±3.1，是在 16px 下箭头仍能分辨的最小值——再小就退化成一条横线。

### 18.4 落地改动

- `plugin/assets/handoff-board.svg` 整体替换（`plugin.json` 的 `interface.composerIcon`
  与 `interface.logo` 都指向它，一处改两处生效）。
- `plugin/.codex-plugin/plugin.json` 版本 `0.3.5` → `0.4.0`：**插件缓存是拷贝不是软链**，
  不改版本号 `codex plugin add` 会认为已装而跳过，侧栏仍是旧图标。
- 两个 home 都重装：`codex plugin remove handoff-board@codex-quota-guard` +
  `codex plugin add handoff-board@codex-quota-guard`，缓存落到
  `.../plugins/cache/codex-quota-guard/handoff-board/0.4.0/`。
- 旧 `0.3.5` 缓存目录由 `plugin remove` 一并清掉，两个 home 只剩 `0.4.0`。

### 18.5 验证

- SVG 用 `xml.etree.ElementTree` 解析通过；`cairosvg` 渲 16/20/24/32/48/64px 均无裁剪。
- 源文件与两个 home 缓存里的 `handoff-board.svg` sha256 一致：
  `1700933c3b7e92e20e2708eeb5b188377bfedce3a0b8223920c0b8f181b05fa4`。
- `codex plugin list` → `handoff-board@codex-quota-guard  installed, enabled  0.4.0`（两个 home）。
- ~~图标只依赖 `currentColor`，跟随主题自动反色~~ —— **这条结论是错的**，第 19 节推翻了它：
  宿主用 `<img>` 加载图标，`currentColor` 在里面根本不生效。详见 `## 19.`。
- 对照图落在 `docs/`：`icon-compare.png` 是同一侧栏行里「旧 / 新」并排（浅色 + 深色，20px，
  2× DPR 渲染后降采样）；`icon-pixels.png` 是 16 / 20 / 24px 的像素真值放大 10 倍，
  用来看小尺寸下线条有没有粘连。以后换图标照这两个尺寸验即可。
- 插件重装后 hook 未受影响，实机 `codex exec` 仍新增
  `SessionStart` / `UserPromptSubmit` / `Stop` 三条（`verdict=OK r5=88.0`）。

## 19. 图标颜色要能在深色和浅色下都看见（2026-11-13）

### 19.1 问题：`currentColor` 在 `<img>` 里是失效的

第 18 节把图标写成了 `stroke="currentColor"`，并假设「桌面版侧栏随主题切换，图标自动反色」。
这个假设从来没有成立过。用 Chrome headless 渲染同一个 SVG 的两种加载方式：

| 加载方式 | `currentColor` 的结果 |
| --- | --- |
| inline `<svg>` 直接嵌在 DOM 里 | 跟随父级 `color`，正常反色 |
| `<img src="x.svg">` | **不生效**，没有 CSS 继承上下文，stroke 落到 SVG 默认黑 `#000` |

宿主走的是第二种：`plugin.json` 的 `interface.composerIcon` / `interface.logo` 是路径字符串，
桌面版按图片资源加载。所以深色主题下图标是黑压黑——实测对比度只有 **1.23:1**，等于看不见。

### 19.2 试过的三种「自动适配」写法，都不行

用 Chrome headless（`--blink-settings=preferredColorScheme=1|2`）在真实深浅背景上截图对比：

| 写法 | 结果 |
| --- | --- |
| `stroke="currentColor"` | 深色背景 1.23:1，不可见。**淘汰** |
| SVG 内嵌 `<style>@media (prefers-color-scheme: dark){svg{stroke:#f2f2f7}}</style>` | `<img>` 里**能用**，深色确实变白。但它只跟**系统**主题，跟不了应用内主题切换；插件 manifest 也没法把宿主的主题传进这个独立 SVG 文档。不可控，**淘汰** |
| `stroke="light-dark(#18181b,#f2f2f7)"` | `<img>` 里 CSS 函数在无 `color-scheme` 的独立文档里回落成黑。**淘汰** |

`interface` 里虽然还有 `logoDark` 字段（二进制 `core-plugins\src\manifest.rs:551` 附近确认），
但它只能覆盖 `logo`，管不到 `composerIcon`，而侧栏用的是后者。

### 19.3 决定：固定中性灰 `#7c7c82`

既然拿不到主题信息，就选一个在两种背景上都不吃亏的中间值：

- 浅色背景（`#ffffff`）对比度 **4.15:1**
- 深色背景（`#1c1c1e`）对比度 **4.10:1**

两边几乎相等，且都过了 WCAG AA 对「非文本图形」要求的 3:1。选 `#7c7c82` 而不是更深的
`#6b6b70`（浅色 5.30:1 / 深色 3.21:1）或更浅的 `#9a9aa0`（浅色 2.80:1 / 深色 6.08:1），
是因为要让两边的**视觉重量**接近——否则图标会在其中一个主题里显得比旁边文字重。
四个方案并排、五种灰阶在深浅背景上的完整对比见
`docs/icon-theme-candidates.png`（上四行是 A/B/C 与 `#6b6b70`，往下依次加深到 `#a0a0a6`）。

代价说清楚：图标不再跟随主题反色，在浅色下比纯黑文字「淡」，在深色下比纯白文字「淡」。
这是有意的——侧栏图标本来就该比文字轻一档。

### 19.4 落地改动

- `plugin/assets/handoff-board.svg`：`stroke="currentColor"` → `stroke="#7c7c82"`。
- `plugin/.codex-plugin/plugin.json`：`0.4.0` → `0.4.1`（缓存是拷贝，不升版本不会重装）。
- 两个 home 重装，缓存落到 `.../handoff-board/0.4.1/`。

### 19.5 验证

- 三方 sha256 一致（源 + 两个 home 缓存）：
  `c1efd8b81a17995cee218b0e15b709ea7dcfdbdadc295d7c6ebe461c1a4bf5f2`。
- 对比图 `docs/icon-theme.png`：左右两栏分别是浅色/深色侧栏行，上两行旧图标、下两行新图标。
  旧图标在深色栏里几乎消失，新图标两边都清晰。
- 插件重装不影响 hook：实机 `codex exec --skip-git-repo-check -C /tmp "reply with the single word: ok"`
  返回 `RC=0`，`hook.log` 依次新增 `SessionStart` / `UserPromptSubmit` / `Stop`
  （`verdict=OK r5=30.0`）。
- 对比度（WCAG 相对亮度公式）：按色值算，新图标浅色 **4.15:1** / 深色 **4.10:1**；
  旧图标 `currentColor` 在浅色是纯黑 21.00:1、深色 **1.23:1**。截图像素实测新图标
  浅色 4.21:1 / 深色 4.10:1（抗锯齿边缘取到 `rgb(123,123,129)`，与色值计算基本一致）。

### 19.6 教训

**别假设 SVG 里的 `currentColor` 会生效——先确认宿主是 inline 还是 `<img>` 加载。**
这个项目里「图标跟随主题」写进过 PLAN 第 18 节，是没验就下的结论；验一次（渲染一张图、
量一次对比度）就推翻了。凡是「看起来应该自适应」的样式特性，都要用真实宿主环境截一次图。
## 20. 面板 UI 重做（2026-11-13）

### 20.1 问题：三栏各说各话

改动前的面板（`docs/panel-ui-compare.png` 左上）有几个具体毛病，都是量出来的，不是感觉：

| 现象 | 根因 |
| --- | --- |
| 左两栏与右栏高度不齐，右栏比左栏高一截 | 左两栏 `ScrollArea` 写死 `h-[26rem]`，右栏 `max-h-[620px]`，三个数互不相干 |
| 长 `HANDOFF.md` 把 `Ask Codex` 按钮顶出视野 | 文档卡是 `max-height`，内容一长整块跟着长，头尾跟着滚走 |
| 文档头部挤成一行 11px 小字 | status / verdict / branch / dirty / 时间 / 完整 id 全塞进同一行 Badge |
| 标题和 `17 projects 664 sessions 4 handoffs` 挤在一行 | stats 是 `h1` 旁边的内联 `<p>`，宽度一变就换行错位 |
| 搜索框固定 `max-w-xs`，和筛选框抢宽度 | 没有 flex 伸缩规则 |
| 选中态在深色下偏弱 | `bg-accent/60` 在深色主题的 accent 上只有微弱差异 |

真实数据也决定了改法：`build_board([])` 实测 **17 projects / 664 sessions / 4 handoffs**，
`dropspot-app` 一个项目就有 235 个 session。所以列表必须能滚、必须能一眼看出「这个项目有没有交接」，
而文档区大部分时间是空的（大多数项目 0 handoff）。

### 20.2 改了什么

四个组件 + 整段 `@layer components`（`src/assets/index.css` 的 `@layer base` 与 `@theme` 未动）。

**`App.vue`**：header 改成 `flex flex-wrap`，左边标题 + 三个 `.hb-chip`（`handoff` 数为 0 时不着色，
`>0` 加 `.hb-chip-accent`），右边搜索与筛选 `ml-auto justify-end`。搜索框换成 `relative max-w-56 min-w-32`
＋内嵌 `Search` 图标；`only with handoffs` 从原生 checkbox 换成 `Button variant="ghost" size="xs"`
＋`ListFilter`，带 `aria-pressed`。

**`ProjectList.vue` / `SessionList.vue`**：`Button` → 原生 `<button class="hb-row">`（省掉 button 的
`inline-flex items-center` 对两行布局的干扰）。每行固定两段：主行（名称 + 右侧 `.hb-dot` 计数）＋
`.hb-meta`（`65s · 4m ago`）。orphan handoff 行加 `FileText` 图标 + `handoff only` 标签，与 session 行区分。
列表头改成 `.hb-col-title` + `.hb-col-count` + `.hb-col-project`（右侧显示所属项目，避免在窄栏里
反复出现项目名）。

**`HandoffDoc.vue`**：整块变成「固定头 / 滚动体 / 固定脚」的 `.hb-card`：

- `.hb-doc-head`：多份交接时的切换按钮组、标题（`goal || handoffLabel`）、副行（slug · 本地时间
  `YYYY-MM-DD HH:mm`）、右上 `Ask Codex`、`.hb-doc-tags`（`partial` / verdict / `branch@head` /
  `uncommitted` / agent）。
- `.hb-doc-body`：唯一的滚动容器。
- `.hb-doc-foot`：等宽字体的 `handoff.dir`。

顺带把 `dirty` 文案改成 `uncommitted`——`dirty` 是 git 术语，但面板读者不一定知道。

**`index.css`**：新增 `.hb-board`（承载 `--hb-pane: clamp(20rem, 58vh, 44rem)`）、`.hb-list`、
`.hb-row` / `.hb-row-on`、`.hb-meta` / `.hb-sep` / `.hb-dot` / `.hb-glyph`、`.hb-chip`、`.hb-tag` 系列、
`.hb-card` 系列；`.hb-md` 排版加强（`h2` 加下边框、`li::marker` 着色、`th` 加粗）。选中态改成
`color-mix(in oklab, var(--primary) 12%, transparent)` 背景 + `inset 2px 0 0 var(--primary)` 左轨——
左轨在深浅两个主题下都稳，不依赖 accent 的明度。

### 20.3 三个坑（都已踩过并修掉）

1. **`max-height` 的卡片撑不开滚动体**。`.hb-card` 最初用 `max-height: var(--hb-pane)`，
   内容少时卡片塌成 192px，`ScrollArea` viewport 只有 66px 却要装 2727px 内容（探针实测
   `card h=192, body h=66, vp scrollHeight=2727`）。必须改成 `height: var(--hb-pane)` 定高，
   再让 body `flex: 1 1 0; height: 0; min-height: 0`、viewport `position: absolute; inset: 0`。
2. **`--blink-settings=preferredColorScheme` 在这版 Chrome headless 里不生效**。浅色/深色两次截图
   字节数完全相同（`362202` / `362202`）。可靠做法是复制渲染产物，把 `<html>` 加上 `class="dark"`、
   把 `@media (prefers-color-scheme: dark)` 改成 `@media all`（深色）或 `@media not all`（浅色）。
   这也正是宿主的真实行为（`[data-theme="dark"]` / `.dark`）。
3. **`index.css` 拼接脚本不幂等**。`hb_copy.sh` 早期版本用 `split(marker)` 拼 components 块，
   第二次运行时 marker 已不存在，于是整段被重复追加（13628 → 23204 字节 / 519 → 924 行）。
   现在改成「保留第一个 marker 之前的 head + 重新追加」，重复跑结果恒定。

### 20.4 验证

- `npx vue-tsc -b` 通过；`npx vite build` → `dist/panel.fragment.html 187.50 kB`（gzip 60.90 kB）。
- 布局探针（往渲染产物注入 `document.title='PROBE '+JSON`，用 `--dump-dom` 读回）：
  1180x820 下 `board h=443 top=76`、`list h=421`、`card h=421`、`head h=99`、`body h=294`、`foot h=26`
  ——三栏底边对齐。
- 截图（`--force-device-scale-factor=2`）：1180 浅色 / 1180 深色 / 900 深色 / 560 单列，
  外加一个「所有项目 0 handoff」的空态。560px 下自动单列，两个列表各 16rem，文档卡在下方。
- 空态不再是大片空白：显示「`<项目名>` has no `.handoff` documents yet.」＋ 交接文档的落点说明。
- 三方 sha256 一致（源 + 两个 home 缓存 0.4.2）：
  `2dae43842afc3af948f10e9148c105cd60b0bfca058a9636f2b0abea05f924a6`。
- 对照图落在 `docs/`：`panel-ui-compare.png`（before/after × 浅色、深色、560 单列四格）、
  `panel-ui-empty.png`（空态）。

### 20.5 教训

**UI 尺寸问题要量，不要看。** 「高度不齐」这类现象背后往往是三个互不相干的硬编码高度；
注入一个探针把 `getBoundingClientRect()` 读回来，比对着截图猜快得多。
另外，任何「主题相关」的验证都要先确认截图工具真的切换了主题——这次差点用两张一模一样的图
（字节数相同）当成了深浅对比。

## 21. 切换账号后仍读上一个账号的额度

### 21.1 现象与根因

用户报告：切换 Codex 账号后，原有会话继续跑，护栏还在报上一个账号的额度；要过一会儿、
或者再「继续」一次才更新。

三个缺口叠在一起：

1. `gate.json` 与所有快照里都**没有账号身份**，护栏根本不知道登录变了。
2. `fetch_quota()` 优先读 rollout 尾部的 `rate_limits`，而那一行是切换前由旧账号写的；
   `_is_stale()` 只看 `primary.resets_at < now`，于是这行「看起来新鲜」的旧数据一路通过，
   app-server 永远不会被问。
3. `h_user_prompt` 只在 `not fresh_enough`（`cache_ttl_seconds = 60`）时刷新，
   `h_pre_tool_use` 只在 `age > cache_ttl` 时刷新——切换后的 60 秒是盲区。

实测现场：`auth.json` 是 `c843f6cd…`（plus），`gate.json` 却是 `plan_type: team`、`r5 = 69.0`，
数据来自 rollout `10-52-19-01a1d2d1`，其 `creator_account_id = f4f309a8…`。两个账号串了。

### 21.2 改法

- **认账号**：`auth_candidates()` 覆盖 `$CODEX_HOME/auth.json`、`~/.codex/auth.json`、
  `/mnt/c/Users/*/.codex/auth.json`，取 mtime 最新的那个读 `tokens.account_id`；
  `auth_state()` 按 `path:mtime` 缓存，只缓存 id 与 mtime，**绝不缓存或打印 token**。
- **记切换点**：`account_state(gate)` 返回 `(account, not_before, changed)`。
  账号一变，`not_before` 就是 auth.json 的写入时刻——比它更早写的额度行属于上一个登录。
- **溯源每一行**：`tail_rate_limits_record()` 在返回 `rate_limits` 的同时返回该记录的
  `timestamp`（原来只返回内层 dict，无从判断写作时刻）。`from_rollout()` 据此丢弃
  `written_at < not_before` 的行，并给快照打上 `account` / `written_at`。
- **归属兜底**：`rollout_creator()` 读 `session_meta.payload.creator_account_id`，
  但它是**会话开始时**盖的章，不能硬门禁切换后写的行——所以只在没有 `not_before`
  可依据时才用它判断归属；缺失 creator 一律放行。
- **挑文件**：`newest_rollout(account, not_before)` 不能只看 mtime。文件可能横跨一次切换
  （旧账号开的会话被新账号接着写），所以当 mtime 落在切换点附近时，改看尾部额度行自己的
  时间戳再判归属。
- **降级而不是误判**：`refresh()` 把 `gate["account"]` 与 `gate["account_stale"]` 落盘；
  **切换点之前取到的**快照若归属和当前账号不一致，判定强制降级为 `UNKNOWN`
  （`account_switch_result()`：`登录账号已切换，现有额度属于上一个账号，需重新读取`），
  `guard_block()` 与 `human_status()` 都写明「上一个账号」。
  判据是快照自己的 `captured_at`：切换之后才取到的数据不是「上一个账号的」，不该因此降级。
- **打破 TTL**：`h_user_prompt` / `h_pre_tool_use` 一旦发现账号变化，立即
  `refresh(..., force_app_server=True)`，不再等 60 秒缓存过期。
- **权威来源**：`quota_status.py --json` 现在输出顶层 `accountId`，`from_app_server()` 把它
  带进快照——app-server 是切换后唯一权威的归属证据。
- **`status` 也跟着判断**：读到 `auth.json` 的账号和 `gate` 里记的不一样就重读，
  不必等 `--refresh`；另加 `--fresh` 直接问 app-server。

### 21.3 坑

1. **整秒截断**。rollout 里的 `timestamp` 常被截到整秒，切换瞬间写的行会看起来「早于」切换点，
   结果当前账号自己的第一行被当成旧账号的丢掉。加 `SWITCH_SLACK_SECONDS = 2.0` 余量后消失。
   没有余量时现象是：切换后第一次读到正确额度，第二次又退回旧值。
2. **`creator_account_id` 不是每行都有**，只有 `session_meta` 一条记录带；
   而 `turn_context` / `token_usage_record` 都没有。所以它只能当「会话从谁开始」的线索，
   不能当「这行是谁写的」的证据。
3. **mtime 不等于写作时刻**。切换后旧会话文件仍可能被 touch（追加日志行、桌面端写状态），
   只看 mtime 会把旧账号的额度当成最新的。
4. **两个 CODEX_HOME 会各登一个号**。`~/.codex/auth.json` 与
   `/mnt/c/Users/*/.codex/auth.json` 是两份独立的登录状态，桌面版和 CLI 可能不是同一个账号。
   所以「app-server 回报的 `accountId` 与 auth.json 不一致」不等于数据过期——它可能只是
   从另一个 home 问到的。这种情况按 `captured_at` 判断：切换后取到的照用，并在
   `status` 里标注 `（额度来自 xxxxxxxx）`；只有切换前取到的才算上一个账号的。否则护栏会
   永远卡在 `UNKNOWN`，比读到「另一个已登录账号」的额度更糟。

### 21.4 验证

- `python3 scripts/quota_guard.py selftest` → `SELFTEST OK`（新增 20 项：时间戳解析、
  切换前后行接受/拒绝、creator 兜底、`newest_rollout` 跳过他人会话、跨切换文件、
  auth 切换检测、降级为 `UNKNOWN` 与恢复）。
- 端到端探针（临时 state 目录 + 桩 app-server）：切换前 `CRITICAL/91%`，切换后一次
  prompt 即变 `OK/5%`，且 app-server 只被调 1 次；切换后只有旧账号的额度行且 app-server
  不可达时，判定 `UNKNOWN` 并输出「上一个账号」；新账号写出第一行后自动恢复。
- 线上实跑：`status` 由 `team / r5 69.0` 变为 `账号: c843f6cd` + app-server 读数，
  `quota_status.py --json` 的 `accountId` 与 `auth.json` 一致。

### 21.5 教训

**额度是「账号 + 时间窗」的函数，缓存键里没有账号就是错的。** 这里的 bug 不是取数失败，
而是「取数成功但取错了对象」——比失败更危险，因为所有健康检查都显示正常。
凡是跨身份复用的缓存，都要把身份写进缓存值并在读时比对，而不是只比时间。

## 22. Ask Codex 按钮「点了没反应」（2026-11-13）

### 22.1 现象与根因

用户报：面板交接文档处的 `Ask Codex` 按钮点了没反应。

第一直觉是「宿主没接住点击」，但桌面端日志证明**点击是通的**：

```
03:32:15.911 Handling app request method=sendFollowUpMessage sandboxId=source-a8941610a48fd0a2
03:32:15.965 Submitting app message mcpAppId=session:a6d6e22b-14e7-404a-b39f-16a71abe786d
03:32:15.965 Queued app message messageId=0e93a780-9dd6-475d-8f47-1b6d422c6584
```

对应会话的 rollout 里也确实落了 `source":"mcp_app"` 的消息、`turn_trigger":"mcp_app_follow_up"`，
新 turn 起来后读了 `HANDOFF.md`。功能链路从头到尾是好的。

真正的问题在面板侧，两个：

1. **返回值被丢掉**。旧 `askCodex()` 是 `async` 且 `return 'sent' | 'copied' | 'failed'`，
   而调用方 `@ask="askCodex"` 直接忽略返回值，界面上没有任何反馈——成功、复制、失败长得一模一样，
   都是「没反应」。
2. **失败被静默吞掉**。宿主在 turn 正在创建时是**抛错**的：

   ```js
   if (t.get(h.mX) || g?.status === "creating" || g?.status === "outcome-unknown")
     throw Error("A message is already being submitted to this chat");
   ```

   旧代码 `send({ prompt, title })` 没有 `.catch`，连点三次就有三次 `Failed to submit app message
   errorMessage="A message is already being submitted to this chat"`，全部变成未处理的 Promise 拒绝。

另外 `window.openai.sendFollowUpMessage` 是个**只增不减**的接口：只要宿主存在就返回一个
`Promise`，拒绝时也没有任何 UI 体现，面板不自己报就永远安静。

### 22.2 改法

目标从「让宿主接住」改成「**不管宿主怎么回，面板都要当场说出结果**」。

**`src/lib/board.ts`**：`askCodex()` 改成同步函数，返回 `AskState`（`idle` / `sending` / `sent` /
`copied` / `failed`），并新增 `askState` / `askNotice` / `lastAskPrompt` 三个 ref 与 `setAsk()`。

- 先 `setAsk('sending', ...)` 再发请求，点击瞬间就有反馈。
- `send()` 外面套 `try/catch`（同步抛错也要接住），返回值是 thenable 就 `.then(成功, 失败)`，
  拒绝时走 `failureNotice(error)` 把宿主的原话翻译成可读文案：
  `already being submitted` → `Codex is still starting the last turn — copy the prompt instead.`，
  `disabled for this view` → `This view blocks follow-ups — copy the prompt instead.`，
  其余 `Codex rejected the request: <原文>`。
- `askLockUntil` 做 2 秒去抖，避免用户连点把宿主的并发提交撞出上面那个错。
- 状态 6 秒后自动清（失败 10 秒），避免陈旧提示一直挂着。

**`copyText()` 用 `document.execCommand('copy')` 而不是 `navigator.clipboard`**：前者是同步的，
不丢用户手势；后者返回 Promise，在 `sandbox="allow-scripts"` 的 iframe 里可能被拒绝。
两条路都走：先同步 `execCommand`，失败再试 `navigator.clipboard.writeText`。

**`src/components/board/HandoffDoc.vue`**：标题右侧变成 `Copy`（ghost）+ `Ask Codex`（primary），
`busy` 时按钮禁用并显示 `Asking…`；标题下方一行 `role="status"` `aria-live="polite"` 的
`.hb-doc-notice`，用 `data-state` 上色（sent/copied 绿、failed 橙）。复制彻底失败时，
文档脚部把 prompt 换成只读 `<textarea>`，用户还能手动选中。

### 22.3 坑

1. **`userActivation` 双闸**。宿主 iframe 侧与父页面侧各有一道
   `navigator.userActivation?.isActive === true`，任何 `await` 都会丢掉瞬时激活，消息被静默丢弃。
   所以 `askCodex()` 必须是同步的，复制也优先走同步 `execCommand`。
2. **`text/html+skybridge` 与 `profile=mcp-app` 是两条不同的桥**。这个插件声明的是 skybridge，
   但桌面端实际按 MCP app 跑（日志有 `Started app widget` / `IAB_LIFECYCLE
   ownerRoutePath=/mcp-app/...`），所以拿到的是 MCP app 的 `sendFollowUpMessage`（会真的提交消息），
   而不是 skybridge 那条最终落到 `globalThis.prompt()` 的老路。
3. **Chrome headless 视口最小 500px**。`--window-size=420` 得到的是 `innerWidth=500`，
   420px 截图必须用「外层 wrapper 页面里放一个 420px 宽的 iframe，再裁切」。
4. **`--blink-settings=preferredColorScheme=1` 在这版 Chrome 无效**（见 20.3 坑 2），
   深色只能靠 `--force-dark-mode`，且两个 flag 同时给时后者生效。
5. **插件缓存有两份**。桌面端读的是 Windows 侧
   `C:\Users\<user>\.codex\plugins\cache\...`，改完 `plugin/` 必须同步过去，
   否则面板还是旧的（见 15.7 坑 1）。

### 22.4 验证

- `npx vue-tsc -b` 通过；`npx vite build` → `dist/panel.fragment.html 190.73 kB`（gzip 61.97 kB）。
- 三态截图（注入 `window.openai` 桩 + 自动点击）：`ok` 绿色 `Sent · Codex starts a new turn in this
  chat`；`reject` 橙色 `Codex is still starting the last turn — copy the prompt instead.` 且脚部
  出现只读 prompt；`nohost` `Copy failed — the prompt is shown under the document`。
- 真实渲染链路（`render.py` 的 visualize shell）在 1100 / 736 / 420 三个宽度都无横向溢出，
  420px 单列下 `Copy` / `Ask Codex` 与标题同排不换行。
- 三方 sha256 一致（源 + WSL 缓存 + Windows 缓存 0.4.2），
  `assets/panel.fragment.html` md5 `73c2e810f68189775e00e1a2b6cef602`。
- `docs/panel-1100-dark.png`、`panel-736.png`、`panel-420.png` 全部重拍。

### 22.5 教训

**「没反应」不是「没生效」。** 用户看到的是结果，不是链路；一个丢弃返回值的 `async` 处理函数
会让「成功」和「失败」在界面上完全同形。凡是「点击 → 外部系统 → 结果」的路径，都要有一个
当场可见的状态位，而不是把返回值交给空气。


---

## 23. DSH 面板「发起 DSH 会话」报 `DSH 会话服务不可用`

### 23.1 现象与根因

DSH 侧栏面板渲染正常，点按钮后标题下出现橙色
`DSH 会话没有启动：DSH 会话服务不可用` —— 说明 `installBridge` 跑了，但拿不到
`uiWorkspace`。根因是桥接层自己发明的 `ClientContext.get()`：

```ts
const uiWorkspace = ctx.get('uiWorkspace') as UiWorkspaceService | undefined
if (!uiWorkspace || typeof uiWorkspace.startSession !== 'function') {
  throw new Error('DSH 会话服务不可用')
}
```

cordis 里 `ctx.get(name)` 确实存在（reflect mixin），但它 **默认 strict**：

```js
get(name, strict = true) {
  return getTraceable(this.ctx, this._getImpl(name, strict)?.value)
}
_getImpl(name, strict = true) {
  const key = this.ctx[symbols.isolate][name]
  const impl = key && this.store[key]
  if (!impl) return
  if (strict && impl.ctx.fiber.state !== 2) return   // <-- ACTIVE 才返回
  return impl
}
```

`fiber.state !== 2`（还没 ACTIVE / 正在迁移）时它 **静默返回 `undefined`**，不抛错。
官方插件从不这么写，一律用 `inject` 作用域上的普通属性访问：

```js
ctx.uiWorkspace.startSession(void 0, { agentPreset: "cordis", prompt, whenDraftExists: "append" })
ctx.layout.selectPanel(id)
```

`Context.extend(meta)` 是 `Object.create(getTraceable(this, this))` —— 子 context 靠原型链继承，
所以 `scoped.uiWorkspace` 能直接命中；而 `ctx.get()` 走的是 store + strict 检查，两者不是一条路。

### 23.2 改法

`panel/handoff-board-panel/src/dsh/client.ts`：

1. **三层兜底解析** `resolveService<T>(ctx, name)`：先普通属性访问（官方写法，最可靠），
   再 `ctx.get?.(name)`（reflect mixin），最后 `ctx.reflect?.get?.(name, false)`
   （绕过 strict 直接读 store）。任一层拿到就返回，全失败才返回 `undefined`。
2. **每次点击时解析**，不在 `apply()` 时缓存：服务可能在插件启动早期还不存在，
   而 bridge 的生命周期比这个窗口长得多。
3. **立刻装桥**：`installBridge(ctx)` 先用插件自身 ctx 装一次，再用
   `ctx.inject(['uiWorkspace'], scoped => installBridge(scoped))` 升级覆盖。
   这样按钮永远点得到 bridge，报错也一定是真实原因，而不是 `startSession === null`。
4. **失败时自诊断**：`describeServices(ctx)` 读 `ctx.reflect.props` 的 key，
   控制台打印 `[handoff-board] uiWorkspace unavailable; known services: ...`。

### 23.3 坑：`selectPanel(null)` 会吞掉刚创建的会话

第一版在 `startSession()` 之后补了一句「切回会话区」：

```ts
await uiWorkspace.startSession(workspaceId, { prompt, whenDraftExists: 'append' })
guarded('reveal conversation', () => resolveService<LayoutService>(ctx, 'layout')?.selectPanel(null))
```

这句是**多余且有害**的。`startSession` 内部走 `openWorkspace(target, beforeOpen, options)` →
`openWorkspaceSession`，它先取一个导航信号：

```js
async openWorkspaceSession(workspaceId, beforeOpen, reuse) {
  const navigation = AbortSignal.any([this.ctx.layout.beginNavigation(), this.lifetime.signal])
  ...
  sessionId = await this.connectBlank(workspaceId, reuse)
  if (navigation.aborted) return          // <-- 被 abort 就直接放弃
  this.replaceMain(sessionId, navigation, "reveal", beforeOpen)
}
```

而 `beginNavigation()` / `selectPanel()` 都会 `this.navigation.abort()`。
所以在 `await` 之后再调 `selectPanel(null)`，等于把这次导航判死，会话建了但不显示。
真正需要的那次 reveal 已经在 `replaceMain` 末尾：

```js
if (panel === "reveal") this.ctx.layout.selectPanel(null)
```

`startSession` 传的正是 `"reveal"`，所以删掉我们那行即可。

另外 `startSession` **不是 async**、不返回 Promise，失败只走
`notify({ kind: "createFailed", message })` 的 toast。所以 `await` 它没有意义，
真正的失败信号是 toast，不是 rejection。

### 23.4 验证

- `npx vue-tsc -b` 通过；`bash scripts/build-dsh-client.sh` → `lib/client.js 197052 B`。
- 三处副本（`dsh-plugin/lib`、`AppData/Local/dsh-handoff-board-stage`、
  `.dsh/profiles/desktop/node_modules/dsh-handoff-board`）内容一致，
  均含 `uiWorkspace unavailable` / `known services` / `whenDraftExists` / `sidebar.panellist`。
- `dsh.client` 清单在入口启动时读取，**装完必须重启 DSH**（见 `dsh-plugin/README.md`）。

### 23.5 教训

**不要给宿主 API 发明签名。** `ctx.get('uiWorkspace')` 看起来天经地义，实际是拿
strict 反射去撞一个还没 ACTIVE 的服务，返回 `undefined` 而不是报错 —— 于是错误被推迟到
我们自己的 `throw`，把「服务还没就绪」伪装成「服务不存在」。凡是写宿主插件，先照着宿主
自己的插件源码抄调用方式（这里是 `ctx.uiWorkspace` / `ctx.layout` 普通属性访问），
再考虑兜底。

## 24. DSH 插件：把面板挂进侧栏（2026-11-13）

需求（m00003）：面板已经能在 Codex 里用，但**在 DSH 里没有入口**。要的是
「侧栏菜单区多一项 → 点开是面板 → 面板交接文档处能直接发起 DSH 会话，让 DSH 按
`HANDOFF.md` 继续干活」。

### 24.1 形态：一个包，两半

DSH 插件是一个 npm 包，`package.json` 里两段清单各管一半：

```jsonc
"dsh": {
  "bundle": { "patch": "./cordis.patch.yml" },   // 宿主半：往 cordis 图里插一行
  "client": { "inject": [...], "platform": "web" } // 浏览器半：进 window.__DSH_BOOT__
}
```

- **宿主半** `lib/index.js`：只干一件事 —— 注册 `/handoff-board` 前缀路由，返回面板 JSON。
  数据采集是 `plugin/scripts/handoff_board.py` 的 Node 移植（`collectSessions` /
  `collectHandoffs` / `loadWorkspaces`），3 秒缓存，`/health` `/board` `/` 三个端点。
- **浏览器半** `lib/client.js`：`sidebar.panellist` 图标行 + `main` keyed 面板，
  面板里 `createApp()` 挂真正的 Vue 板子。

宿主侧不碰 UI，浏览器侧不碰文件系统 —— 中间只有一条同源 `fetch('/handoff-board/board')`。
这比 Typert Remote 那套 RPC 简单得多，而且插件自注册的路由**不经过 launch token 鉴权**
（`GET /` 是 401，`GET /handoff-board/board` 是 200）。

### 24.2 侧栏那一行：`sidebar.panellist`

红框位置（「新会话」下方，「插件 / 自动化任务 / 更多」）不是 `sidebar.footer.action`
（那是 kanban 用的底部位置），是 root 作用域的 list slot **`sidebar.panellist`**：

```ts
ctx.slots.register({
  name: 'sidebar.panellist',
  scope: 'root',
  id: 'handoff-board',
  order: 20,
  label: '交接板',
  component: HandoffBoardIcon,   // 图标组件，props { size: 16 | 18, active }
})
```

参数是 `id` / `order` / `label`，本机 rc 版**不认** `key` / `priority`（照着
`dsh-client-ui-schedule` 抄的）。

### 24.3 面板本体：`main` 必须用 `key`

点菜单后 layout 会去 `main` 槽里找 `entry.options.key === <panellist 的 id>` 的条目，
所以面板注册时**必须**写 `key:` 且与菜单项 `id` 同值：

```ts
ctx.slots.register({
  name: 'main',
  scope: 'root',
  key: 'handoff-board',
  component: HandoffBoardPage,
})
```

`HandoffBoardPage` 是 React 外壳（DSH 面板槽要 React 组件），里面
`createApp(PanelApp).mount('#hb-root')`，把 Vue 那套原样搬进去。

### 24.4 发起会话：桥接，以及两个致命细节

面板跑在 DSH 的 webview 里，不能直接调宿主服务。client 半在 `apply()` 时写一个全局桥：

```ts
window.__HANDOFF_BOARD_DSH__ = { boardUrl, startSession }
```

面板侧 `askDsh()` 优先走它，没有才退回原来的 Codex 剪贴板路径。

**细节一：`ctx.get('uiWorkspace')` 会骗人。** cordis 的 `ctx.get(name)` 默认 strict：
提供服务的 fiber 还没 ACTIVE 时**静默返回 `undefined`**，不抛错。于是
「服务还没就绪」被我们自己那句 `throw new Error('DSH 会话服务不可用')` 伪装成
「服务不存在」。官方插件一律用普通属性访问（`ctx.uiWorkspace`，
因为 `Context.extend()` 是原型链继承）。现在的 `resolveService()` 三层兜底：
普通属性 → `ctx.get()` → `ctx.reflect.get(name, false)`，全失败才报错并打印
`known services` 列表。**每次点击时解析**，不在启动时缓存。

**细节二：`startSession()` 之后不要再 `selectPanel(null)`。** 这是真正的 bug。
`startSession` 是同步函数、返回 `undefined`，内部：

```js
async openWorkspaceSession(workspaceId, beforeOpen, reuse) {
  const navigation = AbortSignal.any([this.ctx.layout.beginNavigation(), this.lifetime.signal])
  sessionId = await this.connectBlank(workspaceId, reuse)
  if (navigation.aborted) return          // 被 abort 就放弃，replaceMain 不执行
  this.replaceMain(sessionId, navigation, "reveal", beforeOpen)
}
```

而 `selectPanel()` 和 `beginNavigation()` 都会 `this.navigation.abort()`。
第一版写的是 `await startSession(...)` 然后补一句「切回会话区」：
`await` 一个非 Promise 立刻完成，此时 `openWorkspaceSession` 还卡在 `await connectBlank`，
我们那句 `selectPanel(null)` 把导航判死 → `replaceMain` 从未执行 →
**`beforeOpen` 从未执行 → prompt 永不注入**，只在 Host 侧留下一个空白会话。

```ts
// 正确：什么都不 await，什么都不补
uiWorkspace.startSession(workspaceId, { prompt, whenDraftExists: 'append' })
```

`replaceMain` 末尾本来就有 `if (panel === "reveal") this.ctx.layout.selectPanel(null)`，
`startSession` 传的正是 `"reveal"` —— 切回会话区是宿主自己做的。

### 24.5 行为边界：只预填，不自动发送

自动提交的链路是探明的（`conversation.input.for(actx).actions.submit()`），但**没有做**。
用户明确选了「只预填」：点按钮 = 建会话 + 把 prompt 塞进输入框，回车由人按。
理由是自动发送会立刻开始烧额度，属于行为升级，不该由一次误点触发。

### 24.6 装与改

```bash
cd panel/handoff-board-panel
bash scripts/build-dsh-client.sh    # vue-tsc -b + vite build + wrap

# pnpm 不能从 UNC 路径装，先 stage 到本地盘
cp -r dsh-plugin "$LOCALAPPDATA/dsh-handoff-board-stage"
dsh plugin --profile desktop add "file:$LOCALAPPDATA/dsh-handoff-board-stage"
```

**改完必须重启 DSH**：`dsh.client` 清单在入口启动时读一次，boot 图在 index.html
加载瞬间就固定了，浏览器侧没有热更新消费方。

### 24.7 验证

- `node dsh-plugin/test-host.mjs`（**Windows node**；WSL node 跑出来 handoffs 是 0，
  因为 `~/.codex` 在 `/mnt/c` 而路径翻译走 workspace 前缀）→
  `stats {projects: 8, sessions: 41, handoffs: 8}`。
- `GET /handoff-board/health` → 200 `{"ok":true}`；`GET /handoff-board/board` → 200 / 82.9 KB。
- 侧栏「新会话」下方出现「交接板」（截图 `docs/dsh-panel.png`：8 projects / 41 sessions /
  8 handoffs，右侧 HANDOFF.md 正文 + tags + `Copy` + `发起 DSH 会话`）。
- 点「发起 DSH 会话」→ 视图切到会话页，workspace 选择器是 `dropspot-website`，
  输入框里是完整 prompt（截图 `docs/dsh-session-draft.png`）。
- 会话投影 `session_projcache/sessions/*.json` 核对：`cwd` 正确、`draft` 有内容、
  `turns: 0`（等回车）—— 正是「只预填」的预期。

### 24.8 教训

1. **宿主 API 不要自己发明签名。** `ctx.get()` 看着天经地义，实际是拿 strict 反射去撞
   还没 ACTIVE 的服务，返回 `undefined` 而不是报错。先抄宿主自己的插件怎么写。
2. **同步函数别 `await`。** `startSession` 不返回 Promise，`await` 它等于什么都不等，
   后面紧接着的代码会跑在宿主异步流程的中间 —— 那次 `selectPanel` 就是这么把会话弄丢的。
3. **插件是两半。** 宿主半改完要重启，浏览器半改完刷新就行；但 `dsh.client` 清单变了
   必须重启。分不清这一点会浪费大量时间在「明明装了却没生效」上。

## 25. 侧栏菜单图标：终于换掉了（2026-11-13）

第 18/19 节换了 `plugin/assets/handoff-board.svg`、也改了 `plugin.json` 的
`interface.composerIcon` / `interface.logo`，但侧栏「⋯」菜单里 Handoff Board 那一行
始终是「圆圈里一个齿轮」的通用图标。**原因是这两条链路根本不是同一条。**

### 25.1 宿主从哪里取那个图标

反查 `app.asar`（`Sg` 函数，偏移 385627090 附近）：

```js
let p = O(n, r, d.type),
    f = R(r.icons) ?? a;                       // a = 规整后的 serverInfo
switch (null != l
  ? f = P(r, h.p(N(r, l) ?? null) ?? a)
  : "local" === e && c.fI(n) && (f = R(p?.entrypoint.icons)), d.type)
```

- `a = h.p(serverInfo)` —— **MCP `initialize` 返回的 `serverInfo.icons`**
- `f = R(tool.icons) ?? a` —— **`tools/list` 里 tool descriptor 的 `icons` 优先**，
  没有才回落到 serverInfo
- `h.p` 就是 `zUz7.p`：把 `icons[]` 规整成 `{logoUrl, logoDarkUrl}`，
  **只接受 `src` 以 `https://` 或 `data:` 开头**，按 `sizes` 面积取最大，无 theme 时兼作亮暗兜底
- 渲染走 `ZQu1.R`：`<image href={src}>` 当 mask + `<rect fill="currentColor">`，
  所以最终颜色是**当前文字色**，SVG 里写的 `stroke="#7c7c82"` 会被覆盖

`plugin.json` 的 `interface.composerIcon` / `interface.logo` 只在插件商店卡片、
推荐位、inline flavor 那条路径上用（`composerIcon` 的 schema 是
`{alt, knownAppId, logoUrl, logoDarkUrl}`），**与侧栏 entrypoint 无关**。
所以第 19 节调了半天 SVG 描边颜色，对这个位置一直是无效功。

### 25.2 改法

`plugin/server.py` 里把 SVG 读出来、压掉多余空白、base64 成 data URI，
同时挂到两处：

```python
ICON_SIZES = ["48x48"]   # 原为 "24x24"，公共目录要求图标方形 ≥48×48，已整体放大

def icon_data_uri() -> str:
    payload = (PLUGIN_ROOT / "assets" / "handoff-board.svg").read_bytes()
    compact = b" ".join(payload.split())
    return "data:image/svg+xml;base64," + base64.b64encode(compact).decode("ascii")

def icons() -> list[dict[str, Any]]:
    global _ICONS
    if _ICONS is None:
        uri = icon_data_uri()
        _ICONS = [{"src": uri, "mimeType": "image/svg+xml", "sizes": ICON_SIZES}] if uri else []
    return _ICONS
```

- `tool_descriptor()` 顶层加 `"icons": icons()`
- `initialize` 的 `serverInfo` 换成 `server_info()`，里面非空就带 `icons`

用 base64 而不是 `utf8,` + percent-encoding：SVG 里有 `#`、`<`、`>`，
手写转义容易漏，base64 一次到位。602 字节的 URI，不值得为省这点体积冒险。

### 25.3 换完还要重启，因为缓存

侧栏入口的元数据缓存在
`%USERPROFILE%\.codex\.codex-global-state.json` 的
`electron-owner-atoms:<hash>.mcp-global-entrypoints-v3` 字段里
（不是 localStorage，也不是 leveldb —— 在 leveldb 里搜不到），
条目形如 `{server, pluginId, tool, resourceUri, title, icon, toolScope, resourceRevision}`，
按 `[userId, accountId, hostId]` 分组，最多 64 条。

**旧条目里 `icon` 是 `null`，不重启就一直命中旧的。**
改完 `server.py` + bump 版本 + 重装插件之后，必须把桌面版整个退出再开：

```powershell
Get-Process ChatGPT | Stop-Process -Force
Start-Process "shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"
```

重启后再看这个 JSON，`icon.logoUrl` 已经是
`data:image/svg+xml;base64,...`，`tool.icons` 有一项 —— 这才算真的进去了。

侧栏「⋯」菜单实测（截图 `docs/codex-sidebar-icon.png`）：图标已变成
「板子 + 右向箭头」，与 `assets/handoff-board.svg` 的 `rect` + `path` 对得上，
不再是那个圆圈齿轮。

### 25.4 教训

1. **先确认宿主读的是哪个字段，再动手改资源。** 这次浪费在「改 SVG 描边颜色」
   上的时间，全部源于假设 `plugin.json` 的 interface 字段就是这个图标的来源。
2. **同一份视觉资产往往有好几条渲染链路**（商店卡片 / 侧栏 entrypoint / 面板内 header），
   每条链路的 schema 和取值函数都不一样。要改哪一个，就去 asar 里把那一段读出来。
3. **有持久化缓存的地方，改完不重启等于没改。** 而且缓存不一定在 localStorage 里，
   这次就藏在主进程的 global-state JSON 里。