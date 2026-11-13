#!/usr/bin/env python3
"""把 hooks.json 里 quota_guard 条目的当前 hash 写进 config.toml 的 [hooks.state]。

背景：Codex 对非托管 hook 按**内容 hash** 记信任。`hooks.json` 一改（改名、调 timeout、
换路径、改 matcher），旧 `trusted_hash` 全部失效，hook **静默不执行**——护栏看起来装着，
其实一条都没跑。`trusted_hash` 的值恰好等于 app-server `hooks/list` 返回的 `currentHash`，
但这个 hash 无法从 handler 字段逆推（试过 json.dumps 的各种变体、Rust Debug 串、字段子集、
各种分隔符拼接、二次 hash，全部对不上），所以只能让 codex 自己算：起 app-server 问一次再写回去。

用法：
  python3 scripts/hook_trust.py                       # 只看，不写
  python3 scripts/hook_trust.py --apply               # 写进 $CODEX_HOME/config.toml
  python3 scripts/hook_trust.py --apply --home /mnt/c/Users/x/.codex
  python3 scripts/hook_trust.py --apply --home /mnt/c/Users/x/.codex --codex /path/codex.exe

跨 home 的坑（两边都会踩，所以脚本按 home 形态自己选可执行文件）：
- `/mnt/c/...` 形式的 home **必须**用 Windows 版 codex.exe 去问。在 WSL 里让 Linux 版 codex
  指向 `/mnt/c/...` 会在 sqlite 初始化时失败：`failed to initialize sqlite state runtime
  under /mnt/c/...`（sqlite 跨文件系统不兼容）。WSL 能直接执行 .exe，所以自动切过去，
  并把 CODEX_HOME 转成 `C:\\...` 形式。
- Linux home **绝不能**用 .exe 去问。Windows 版认不出 `/home/...` 路径，会**静默**回退到
  `C:\\Users\\...`，于是打印出另一个 home 的答案、却显示得像本 home 的（踩过这个坑）。
  所以 Linux home 只认 PATH 或 `~/.local/bin` 里的 codex。
- 光靠选择还不够保险，脚本会核对 `hooks/list` 返回的 key 前缀是否真属于这个 home，
  不一致就报错退出，避免把「别的 home 的 trusted」当成自己的。
- 写 key 时注意：Windows 路径里是反斜杠，必须用 TOML **字面量**字符串（单引号）包 key。
  用双引号会被当成转义序列解析，`C:\\Users` 里的 `\\U` 直接报
  `too few unicode value digits`，整份 config.toml 被判为非法、静默回落默认配置
  （踩过这个坑；后果是 hook 信任全丢，比不写还糟）。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

CLIENT_INFO = {"name": "codex_quota_guard_skill", "title": "Codex Quota Guard Skill", "version": "1.0.0"}
INIT_TIMEOUT = 45
LIST_TIMEOUT = 45
WINDOWS_CODEX_GLOBS = [
    "/mnt/c/Users/*/AppData/Local/OpenAI/Codex/bin/*/codex.exe",
    "/mnt/c/Program Files/WindowsApps/OpenAI.Codex_*/app/resources/codex.exe",
]


def home_is_windows(home: pathlib.Path) -> bool:
    """home 是不是 Windows 侧的（`/mnt/c/...` 或 `C:\\...`）。"""
    text = str(home)
    if text.startswith("/mnt/") and len(text) > 6 and text[5].isalpha():
        return True
    return len(text) > 1 and text[1] == ":"


def to_windows_path(home: pathlib.Path) -> str:
    text = str(home)
    if len(text) > 1 and text[1] == ":":
        return text
    try:
        return subprocess.run(
            ["wslpath", "-w", text], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return text


def normalize(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").lower()


def toml_key(key: str) -> str:
    """把 key 写成 TOML 表名。含反斜杠/引号时必须用字面量（单引号）串，避免转义解析。"""
    if "'" in key:
        return '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return "'" + key + "'"


def find_codex(explicit: str | None, windows_home: bool) -> tuple[str, bool]:
    """返回 (可执行文件路径, 是否 Windows 版)。按 home 形态选，不能混用。"""
    if explicit:
        return explicit, explicit.lower().endswith(".exe")
    if windows_home:
        candidates: list[str] = []
        for pattern in WINDOWS_CODEX_GLOBS:
            candidates.extend(glob.glob(pattern))
        if candidates:
            newest = max(candidates, key=os.path.getmtime)
            return newest, True
        raise SystemExit(
            "Windows home 需要 Windows 版 codex.exe 才能问 hooks/list，但没找到。\n"
            "用 --codex 指定路径，或去桌面版 Settings > Hooks 手动信任。"
        )
    found = shutil.which("codex")
    if found:
        return found, False
    fallback = pathlib.Path.home() / ".local" / "bin" / "codex"
    if fallback.exists():
        return str(fallback), False
    raise SystemExit("找不到 Linux 版 codex（PATH 和 ~/.local/bin 都没有）")


def list_hooks(
    codex: str, home: pathlib.Path, windows: bool
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """返回 (属于该 home 的 {key: {...}}, 不属于该 home 的 key 列表)。

    key 形如 `<home>/hooks.json:<snake_event>:<group>:<handler>`，前缀就是 home 自身，
    所以能据此判断 app-server 到底读的是哪个 home。
    """
    env = os.environ.copy()
    env["CODEX_HOME"] = to_windows_path(home) if windows else str(home)
    proc = subprocess.Popen(
        [codex, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )

    def send(payload: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    def wait(rid: int, seconds: int) -> dict | None:
        assert proc.stdout is not None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                return None
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == rid:
                return message
        return None

    try:
        send({"method": "initialize", "id": 0, "params": {"clientInfo": CLIENT_INFO}})
        if not wait(0, INIT_TIMEOUT):
            return {}, []
        send({"method": "initialized", "params": {}})
        send({"method": "hooks/list", "id": 1, "params": {}})
        response = wait(1, LIST_TIMEOUT)
        if not response:
            return {}, []
        prefix = normalize(to_windows_path(home) if windows else str(home)) + "/"
        mine: dict[str, dict[str, str]] = {}
        foreign: list[str] = []
        for entry in response.get("result", {}).get("data", []):
            for hook in entry.get("hooks", []):
                if "quota_guard" not in (hook.get("command") or ""):
                    continue
                key = hook["key"]
                if not normalize(key).startswith(prefix):
                    foreign.append(key)
                    continue
                mine[key] = {"hash": hook["currentHash"], "trust": hook["trustStatus"]}
        return mine, foreign
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        noise = (proc.stderr.read() or "").strip() if proc.stderr else ""
        if noise:
            print(noise[-400:], file=sys.stderr)


def rewrite_state(config: pathlib.Path, entries: dict[str, dict[str, str]]) -> None:
    """删掉旧的 [hooks.state] 块，写回 entries；其它配置项原样保留。"""
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    kept: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if (
            stripped == "[hooks.state]"
            or stripped.startswith('[hooks.state."')
            or stripped.startswith("[hooks.state.'")
        ):
            skipping = True
            continue
        if skipping:
            if stripped.startswith("[") and not stripped.startswith("[hooks.state."):
                skipping = False
            else:
                continue
        kept.append(line)
    body = "".join(kept).rstrip("\n") + "\n"
    block = ["\n[hooks.state]\n"]
    for key, entry in entries.items():
        block.append(f'\n[hooks.state.{toml_key(key)}]\ntrusted_hash = "{entry["hash"]}"\n')
    config.write_text(body + "".join(block), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="把 quota_guard hooks 的 currentHash 写进 config.toml")
    parser.add_argument("--apply", action="store_true", help="真的写入（默认只打印）")
    parser.add_argument("--home", default=os.environ.get("CODEX_HOME") or str(pathlib.Path.home() / ".codex"))
    parser.add_argument("--codex", default=None, help="指定 codex 可执行文件（默认按 home 形态自动找）")
    args = parser.parse_args()

    home = pathlib.Path(args.home)
    windows = home_is_windows(home)
    codex, codex_is_exe = find_codex(args.codex, windows)
    if windows != codex_is_exe:
        print(
            f"可执行文件与 home 不匹配：home={home}（{'Windows' if windows else 'Linux'} 侧），"
            f"codex={codex}（{'Windows' if codex_is_exe else 'Linux'} 版）。\n"
            "Windows home 要用 codex.exe，Linux home 要用 Linux 版 codex，否则读的是别的 home。",
            file=sys.stderr,
        )
        return 2

    entries, foreign = list_hooks(codex, home, windows)
    if not entries:
        if foreign:
            print(
                f"{home}: app-server 返回的 hook 不属于这个 home，实际读到的是：{foreign[0].split('hooks.json')[0]}\n"
                "说明 codex 没认这个 CODEX_HOME。Linux home 请用 Linux 版 codex，"
                "Windows home 请用 codex.exe。",
                file=sys.stderr,
            )
            return 2
        print(
            f"{home}: 拿不到 hooks/list（app-server 没起来，或没有 quota_guard 条目）。\n"
            "手动路径：CLI 里执行 /hooks，桌面版 Settings > Hooks，逐条信任。",
            file=sys.stderr,
        )
        return 2

    trusted = sum(1 for e in entries.values() if e["trust"] == "trusted")
    print(f"{home}: {len(entries)} 条 quota_guard hook，其中 {trusted} 条已信任  ({codex})")
    for key, entry in entries.items():
        mark = "ok " if entry["trust"] == "trusted" else "NEW"
        print(f"  [{mark}] {key}")

    if not args.apply:
        print("\n（只读模式；加 --apply 写入 config.toml）")
        return 0

    config = home / "config.toml"
    backup = config.with_name(config.name + ".bak-hooktrust")
    if config.exists():
        if not backup.exists():
            shutil.copy(config, backup)
            print(f"  备份 -> {backup}")
    else:
        # 全新 home 里 codex 还没写过 config.toml。这里直接建一个只含 [hooks.state] 的文件：
        # codex 会把缺的配置项按默认值补齐，不会因为文件短而判非法。
        config.parent.mkdir(parents=True, exist_ok=True)
        print(f"  {config} 不存在，新建（只有 [hooks.state]）")
    rewrite_state(config, entries)
    print(f"  写入 {len(entries)} 条 -> {config}")

    after, after_foreign = list_hooks(codex, home, windows)
    if not after:
        print(
            "复查失败：写入后 hooks/list 读不到任何 quota_guard 条目。\n"
            + (f"多半是 config.toml 被写坏了，从备份恢复：cp {backup} {config}" if backup.exists() else "")
            + (f"\n（返回的 key 属于别的 home：{after_foreign[0]}）" if after_foreign else ""),
            file=sys.stderr,
        )
        return 1
    if len(after) != len(entries):
        print(f"复查：条目数从 {len(entries)} 变成 {len(after)}，可能被 codex 丢弃了部分", file=sys.stderr)
    still = [k for k, e in after.items() if e["trust"] != "trusted"]
    if still:
        print("仍未被信任：" + ", ".join(still), file=sys.stderr)
        return 1
    print(f"  复查：{len(after)} 条全部 trusted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
