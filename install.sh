#!/usr/bin/env bash
# 安装 codex-quota-guard：symbolic link 技能 + 合并 hooks + 信任 hooks + 写入 AGENTS.md 硬规则。
#
#   ./install.sh                     # 安装到 $CODEX_HOME（未设置时为 ~/.codex）
#   ./install.sh --also /mnt/c/Users/callmesoul/.codex   # 顺带装 Codex 桌面版的 home
#   ./install.sh --no-agents         # 不碰 AGENTS.md
#   ./install.sh --no-plugin         # 不装 handoff-board 插件（只装 hooks/技能）
#   ./install.sh --no-trust          # 不自动写 [hooks.state]（留给人手动信任）
#   ./install.sh --uninstall         # 摘掉 hooks 与软链（保留交接文档）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${SKILL_DEST:-$HOME/.agents/skills/codex-quota-guard}"
HOMES=("${CODEX_HOME:-$HOME/.codex}")
WRITE_AGENTS=1
UNINSTALL=0
INSTALL_PLUGIN=1
TRUST_HOOKS=1

while [ $# -gt 0 ]; do
  case "$1" in
    --also) HOMES+=("$2"); shift 2 ;;
    --no-agents) WRITE_AGENTS=0; shift ;;
    --no-plugin) INSTALL_PLUGIN=0; shift ;;
    --no-trust) TRUST_HOOKS=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# CODEX_HOME may already equal an explicit --also target (common in the desktop app).
# De-duplicate before merge/uninstall so the same hooks.json is never processed twice.
UNIQUE_HOMES=()
for candidate in "${HOMES[@]}"; do
  seen=0
  for existing in "${UNIQUE_HOMES[@]}"; do
    if [ "$candidate" = "$existing" ]; then
      seen=1
      break
    fi
  done
  [ "$seen" = "1" ] || UNIQUE_HOMES+=("$candidate")
done
HOMES=("${UNIQUE_HOMES[@]}")

PY="$(command -v python3 || command -v python)"

# 非登录 shell 的 PATH 常常没有 ~/.local/bin，codex 明明装着却被判成「不在 PATH」，
# 于是插件那一步被静默跳过（缓存还是旧的）。这里补一下，找不到也无所谓。
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) [ -d "$HOME/.local/bin" ] && PATH="$HOME/.local/bin:$PATH" ;;
esac

if [ "$UNINSTALL" = "1" ]; then
  for home in "${HOMES[@]}"; do
    target="$home/hooks.json"
    [ -f "$target" ] || continue
    "$PY" - "$target" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
hooks = data.get("hooks", {})
for event in list(hooks):
    for group in hooks[event]:
        group["hooks"] = [h for h in group.get("hooks", []) if "quota_guard" not in h.get("command", "")]
    hooks[event] = [g for g in hooks[event] if g.get("hooks")]
    if not hooks[event]:
        del hooks[event]
if not hooks:
    data.pop("hooks", None)
json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"removed quota_guard hooks from {path}")
PY
  done
  if [ -L "$DEST" ]; then
    rm -f "$DEST"
  elif [ -f "$DEST/SKILL.md" ] && [ "$(readlink -f "$DEST")" != "$HERE" ]; then
    rm -rf "$DEST"
  fi
  if command -v codex >/dev/null 2>&1; then
    # 每个 home 各有一份插件缓存（见 5/6 的注释），所以卸载也要逐个 home 摘，
    # 否则 --also 指定的那个 home 会留下一个指向已删目录的插件。
    for home in "${HOMES[@]}"; do
      CODEX_HOME="$home" codex plugin remove handoff-board@codex-quota-guard >/dev/null 2>&1 || true
      CODEX_HOME="$home" codex plugin marketplace remove codex-quota-guard >/dev/null 2>&1 || true
      echo "  plugin handoff-board + marketplace codex-quota-guard removed from $home"
    done
  fi
  echo "uninstalled. 交接文档与 state 目录保留。"
  exit 0
fi

echo "== 1/6 链接技能 =="
mkdir -p "$(dirname "$DEST")"
# DEST 逐项软链而不是整棵树：整棵树会把 plugin/skills/handoff-board 也登记成技能，
# 导致 codex 里同一个技能出现两次（r1 与 r3 各一条）。
if [ -L "$DEST" ]; then
  rm -f "$DEST"
elif [ -e "$DEST" ]; then
  if [ ! -f "$DEST/SKILL.md" ] || [ "$(readlink -f "$DEST")" = "$HERE" ]; then
    echo "refusing to replace $DEST: not a previously installed skill dir" >&2
    exit 1
  fi
  rm -rf "$DEST"
fi
mkdir -p "$DEST"
for entry in SKILL.md PLAN.md scripts config hooks agents; do
  [ -e "$HERE/$entry" ] || continue
  ln -sfn "$HERE/$entry" "$DEST/$entry"
done
echo "  $DEST -> $HERE (逐项软链，不暴露 plugin/)"

echo "== 2/6 自检 =="
"$PY" "$HERE/scripts/quota_guard.py" selftest

echo "== 3/6 合并 hooks =="
for home in "${HOMES[@]}"; do
  mkdir -p "$home"
  "$PY" - "$home/hooks.json" "$HERE" <<'PY'
import json, sys
from pathlib import Path

target = Path(sys.argv[1])
skill = Path(sys.argv[2])
template = json.loads((skill / "hooks" / "hooks.json").read_text(encoding="utf-8"))
rendered = json.loads(json.dumps(template).replace("__SKILL_DIR__", str(skill)))

data = {}
if target.exists():
    try:
        data = json.loads(target.read_text(encoding="utf-8")) or {}
    except json.JSONDecodeError:
        raise SystemExit(f"{target} 不是合法 JSON，先手动修好再来")

hooks = data.setdefault("hooks", {})
data.setdefault("description", rendered["description"])
for event, groups in rendered["hooks"].items():
    existing = hooks.setdefault(event, [])
    for group in existing:                      # 清掉旧版 quota_guard 条目，保证幂等
        group["hooks"] = [h for h in group.get("hooks", []) if "quota_guard" not in h.get("command", "")]
    existing[:] = [g for g in existing if g.get("hooks")]
    existing.extend(groups)

target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"  merged -> {target}")
PY
done

echo "== 4/6 信任 hooks =="
# 非托管 hook 按**内容 hash** 记信任；合并 hooks 一定改了内容，旧 trusted_hash 立刻失效，
# 而失效的 hook 是**静默不执行**的——护栏看起来装着其实一条没跑。trusted_hash 恰好等于
# app-server `hooks/list` 的 currentHash（无法从 handler 字段逆推），所以让 codex 自己算。
if [ "$TRUST_HOOKS" = "0" ]; then
  echo "  skipped (--no-trust)；手动：CLI 里 /hooks，桌面版 Settings > Hooks"
else
  for home in "${HOMES[@]}"; do
    echo "  -> $home"
    if ! "$PY" "$HERE/scripts/hook_trust.py" --apply --home "$home"; then
      echo "  自动信任失败：去 CLI 执行 /hooks，或桌面版 Settings > Hooks 手动信任。" >&2
    fi
  done
fi

echo "== 5/6 安装插件 handoff-board =="
if [ "$INSTALL_PLUGIN" = "0" ]; then
  echo "  skipped (--no-plugin)"
elif ! command -v codex >/dev/null 2>&1; then
  echo "  codex 不在 PATH；手动执行：codex plugin marketplace add $HERE && codex plugin add handoff-board@codex-quota-guard"
else
  # 每个 home 各有一份插件缓存（WSL 的 ~/.codex 与 Windows 的 C:\Users\<you>\.codex 是两个
  # 独立目录，不是同一棵树的两种视图）。桌面版从它自己那个 home 拉起 MCP server，所以只装默认
  # home 会出现「侧栏有按钮、点开空白」。用 --also 指定第二个 home 时这里一起装。
  for home in "${HOMES[@]}"; do
    echo "  -> $home"
    CODEX_HOME="$home" codex plugin marketplace add "$HERE" >/dev/null 2>&1 || true
    CODEX_HOME="$home" codex plugin remove handoff-board@codex-quota-guard >/dev/null 2>&1 || true
    CODEX_HOME="$home" codex plugin add handoff-board@codex-quota-guard
  done
fi

if [ "$WRITE_AGENTS" = "1" ]; then
  echo "== 6/6 AGENTS.md 硬规则 =="
  for home in "${HOMES[@]}"; do
    agents="$home/AGENTS.md"
    touch "$agents"
    if grep -q "codex-quota-guard:begin" "$agents" 2>/dev/null; then
      echo "  already present: $agents"
      continue
    fi
    cat >> "$agents" <<EOF

<!-- codex-quota-guard:begin -->
## 配额护栏
每个任务会话开始时读 \`~/.codex/quota-guard/gate.json\`（不存在就跑
\`python3 $DEST/scripts/quota_guard.py status --refresh\`）。
判定不是 OK 时，按 \`$DEST/SKILL.md\` 的暂停协议收尾：先写交接文档再停。
多步任务开工前跑 \`quota_guard.py plan --items <待办项数>\`，每个里程碑后跑 \`quota_guard.py check --milestone\`。
<!-- codex-quota-guard:end -->
EOF
    echo "  appended -> $agents"
  done
else
  echo "== 6/6 跳过 AGENTS.md =="
fi

cat <<'EOF'

装好了。验证一步：

  codex "随便改个注释"
  然后看 ~/.codex/quota-guard/hook.log 有没有新的 SessionStart / Stop 记录。
  没有记录 = hook 没被信任（静默跳过）。重跑 python3 scripts/hook_trust.py --apply，
  或去 CLI 执行 /hooks、桌面版 Settings > Hooks 手动信任。

改了 hooks.json（调 timeout、改 matcher、换路径）之后必须重跑一次信任，
否则 hook 会静默失效；临时绕过用 codex --dangerously-bypass-hook-trust。

面板（handoff-board 插件）三种开法：

  A) 重启 ChatGPT/Codex 桌面版后，点击全局左侧栏的 Handoff Board 按钮。
     它由插件内置的本地 MCP App 提供，不需要常驻 HTTP 服务。
     MCP Apps 在桌面版里仍是 under development 且默认关闭，每个 home 的
     config.toml 都要加（CLI: codex features enable enable_mcp_apps）：

       [features]
       enable_mcp_apps = true
  B) 会话内问一句「看看交接文档」，Codex 会调用 handoff-board 技能，
     发送内联可视化标记，面板直接渲染在对话里（需要宿主支持内联可视化）。
  C) 浏览器面板：python3 <plugin>/scripts/handoff_board.py serve --port 8787

重新打包后刷新缓存（插件缓存是拷贝不是软链，改源文件必须重装）：
  codex plugin remove handoff-board@codex-quota-guard
  codex plugin add handoff-board@codex-quota-guard
EOF
