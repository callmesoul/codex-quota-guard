# 发布 handoff-board 插件

这个仓库里有两样东西：`codex-quota-guard` 本体（hooks + 技能 + 脚本），和 `plugin/` 目录下的 **handoff-board 插件**。只有后者是「插件」，也只有它能进 Codex 插件市场。

Codex 侧一共有三条互不相同的分发通道，别混着用：

| 通道 | 面向谁 | 要审核吗 | 本仓库状态 |
|---|---|---|---|
| **A. 仓库 marketplace**（`.agents/plugins/marketplace.json`） | 自己 / 团队 / 愿意手动加源的作者 | 否 | ✅ 已就绪 |
| **B. 自己的 ChatGPT workspace** | 同 workspace 成员 | 否（需 workspace admin） | 未做 |
| **C. 公共 Plugin Directory**（ChatGPT + Codex 共用） | 所有人 | 是（5 正 + 3 反测试、demo 录像） | 未做，且被 allowlist 卡住 |

---

## A. 仓库 marketplace（推荐先走这条）

`.agents/plugins/marketplace.json` 已经把 `./plugin` 暴露成一个可安装的插件。任何人（包括你自己换机器后）只要一条命令：

```bash
codex plugin marketplace add callmesoul/codex-quota-guard
codex plugin add handoff-board@codex-quota-guard
```

`marketplace add` 的源可以是：

```bash
codex plugin marketplace add callmesoul/codex-quota-guard          # GitHub owner/repo
codex plugin marketplace add callmesoul/codex-quota-guard@v0.4.3   # 锁 tag
codex plugin marketplace add https://github.com/callmesoul/codex-quota-guard.git
codex plugin marketplace add git@github.com:callmesoul/codex-quota-guard.git
codex plugin marketplace add /path/to/codex-quota-guard            # 本地目录
```

Git 源还能只拉需要的子目录，省带宽。注意 `.agents`（放 marketplace.json）和 `plugin`（放插件本体）**两个都要**，只给 `--sparse plugin` 会报 `marketplace root does not contain a supported manifest`：

```bash
codex plugin marketplace add callmesoul/codex-quota-guard \
  --sparse .agents --sparse plugin
```

安装后验证：

```bash
codex plugin list                    # handoff-board@codex-quota-guard  installed, enabled  0.4.3
codex mcp list                       # 应出现 handoff_board，cwd 指向缓存目录
codex plugin marketplace list        # 列出已加的所有源
```

**改完插件必须重装。** 插件缓存是**拷贝**不是软链，直接改 `plugin/` 不会生效：

```bash
codex plugin remove handoff-board@codex-quota-guard
codex plugin add handoff-board@codex-quota-guard
```

`plugin remove` 必须用完整的 `<plugin>@<marketplace>`，只写 `handoff-board` 会报 `plugin requires --marketplace unless passed as <plugin>@<marketplace>`。

仓库根目录的 `install.sh` 会顺手做这件事（`--no-plugin` 可跳过）。

### 发布新版本

1. 改 `plugin/.codex-plugin/plugin.json` 的 `version`（这是全仓库唯一一处版本号）。
2. 提交并打 tag：`git tag v0.4.4 && git push --tags`。
3. 用户侧 `codex plugin marketplace upgrade codex-quota-guard` 后重装插件。

---

## B. 发到自己的 ChatGPT workspace

只在 workspace 内可见，不进公共目录，不需要审核。

1. 需要 workspace 的 **admin** 角色。
2. `chatgpt.com/plugins` → **Personal** → 右上角三点菜单 → **Publish**。
3. 选择要授予的角色，提交。

---

## C. 公共 Plugin Directory

公共目录是 **ChatGPT 和 Codex 共用的一份**，只能上传一次；上传后两个宿主都能看到。

### C0. 先看这一条：本插件当前进不去

handoff-board 是「**本地 stdio MCP + 技能**」的桌面专属包（`plugin/.mcp.json` 里 `command: python3`，`cwd: "."`）。按 OpenAI 的 `deploy/desktop-only` 规则：

> Local MCP 提交**仅限被批准的合作伙伴**。

所以顺序是：

1. 打开 `platform.openai.com/settings/organization/general`，取 **组织 ID**。
2. 把组织 ID 发给 OpenAI 的对接人，申请 **local MCP allowlist**，等对方确认。
3. 确认后 `platform.openai.com/plugins` → **Create plugin** → **Skills only** → **Upload ZIP**。
4. 补完 submission 要求 → **Submit for review** → 通过后自己在门户点 **Publish**。

第 2 步没批下来之前，C 通道是走不通的 —— 门户里根本不会给你 local MCP 的上传口。这也是为什么建议先走 A。

`plugin/.codex-plugin/plugin.json` 顶层的 `"requires_local_executor": true` **必须保留**：没有它，门户会把 skills-only 包当成 web 插件，导致面板打不开。注意 CLI 0.154.0 内置的 `validate_plugin.py` 还不认识这个字段（会报 `plugin.json field 'requires_local_executor' is not accepted by plugin validation`），这是**本地校验器版本落后**，不是字段写错了 —— 门户校验器接受它。别为了过本地校验把它删掉。

### C1. 硬性限制（提交前自查）

| 项 | 限制 | 本仓库 |
|---|---|---|
| `name` | 小写连字符，≤64 | ✅ `handoff-board` |
| `version` | 必填 | ✅ `0.4.3` |
| `description` | ≤4000 | ✅ |
| `author.name` | ≤120 | ✅ `callmesoul` |
| `interface.displayName` | ≤30 | ✅ `Handoff Board`（13） |
| `interface.shortDescription` | **≤30** | ✅ `Browse handoffs by project`（26） |
| `interface.longDescription` | ≤4000 | ✅ 285 |
| `interface.developerName` | ≤80 | ✅ |
| `interface.category` | 必填 | ✅ `Developer Tools` |
| `interface.capabilities` | ≤20 项、每项 ≤120 | ✅ 2 项 |
| `interface.defaultPrompt` | ≤3 条、每条 ≤128 | ✅ 2 条 |
| `interface.brandColor` | `#RRGGBB`，对白底 ≥2:1 | ✅ `#2563EB` |
| `composerIcon` / `logo` | **方形 ≥48×48**，单文件 ≤5 MiB | ✅ `assets/handoff-board.svg`（48×48） |
| `websiteURL` `supportURL` `privacyPolicyURL` `termsOfServiceURL` | MCP 审核时**四个全要**，HTTPS | ❌ 全缺 |
| `review.test_cases` | 5 个正例 + 3 个反例 | ❌ 缺 |
| `review.demo_recording_url` | 演示录像 | ❌ 缺 |
| `extensions.com.openai.publication` | 发布声明 | ❌ 缺 |

`review` / `publication` 放在 `extensions.com.openai` 下，**不要**塞进 `interface`。

### C2. 不能提交的东西

- **lifecycle hooks**：含 `hooks/` 的包不能进公共目录，只能手动装到 Codex 桌面。本仓库的 `hooks/` 在**仓库根**，不在 `plugin/` 里，所以插件包本身是干净的 —— 打 ZIP 时只打 `plugin/` 内部即可，别把仓库根一起打进去。
- **app 引用**（`apps/`、`.app.json`）：同样只能桌面手动装。本插件没有。

### C3. ZIP 里放什么

```
.codex-plugin/plugin.json
.mcp.json
server.py
scripts/
skills/handoff-board/SKILL.md
assets/
```

`screenshots` 若要用，必须是 PNG 且放在 `assets/` 下。`docs/` 里的 `panel-codex-desktop.png`、`panel-1100.png` 等可以先拷进 `plugin/assets/` 再引用。

### C4. 另一种形态：portable Agent Plugins

如果以后想摆脱 allowlist 限制、走通用插件格式，可以改成 portable 结构：根 `plugin.json`（带 `$schema: https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`）+ `mcp.json`（每个 server 带 `type`）+ 根 `skills/` + `assets/`，OpenAI 专属字段全放 `extensions.com.openai`。

两点注意：

- portable 的 `mcp.json` **才**支持 `${PLUGIN_ROOT}` / `${PLUGIN_DATA}` 占位符，例如 `{"type":"stdio","command":"python3","args":["${PLUGIN_ROOT}/server.py"],"cwd":"${PLUGIN_ROOT}"}`。
- 当前 legacy 格式的 `.mcp.json` **不做**占位符替换 —— 实测把 `cwd` 写成 `${PLUGIN_ROOT}` 会被字面拼进路径（`.../0.4.3/${PLUGIN_ROOT}`）。所以 legacy 保持 `cwd: "."` + `args: ["./server.py"]`。
- `extensions.com.openai` 一旦存在，会**整体取代** `.codex-plugin/plugin.json` 的 OpenAI 设置，两者不合并。

### C5. 更新已发布的插件

- MCP 服务端代码改动：每日自动扫描生效，不用重新上传。
- 元数据 / 技能改动：重新上传 ZIP（同一个插件，换新版本号）。

---

## 本地自查

Codex 二进制里内嵌了一份 `plugin-creator/scripts/validate_plugin.py`，可以抠出来离线跑：

```bash
python3 validate_plugin.py plugin
```

它检查 manifest 结构、`interface` 字段白名单、`.mcp.json` 顶层字段、每个 `skills/*/SKILL.md` 的 frontmatter。目前唯一的报错是 `requires_local_executor`（见 C0 的说明，属预期）。

## 参考

- 构建与 manifest：<https://developers.openai.com/plugins/build/plugins>
- 桌面专属 / local MCP：<https://developers.openai.com/plugins/deploy/desktop-only>
- 官方示例仓库：<https://github.com/openai/plugins>
