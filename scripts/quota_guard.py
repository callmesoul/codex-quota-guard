#!/usr/bin/env python3
"""Codex quota guard: read the account quota, decide whether the current task can finish,
block doomed work, and produce a handoff document another agent can pick up.

Hook entry point (Codex hooks, JSON payload on stdin):

    quota_guard.py hook <EventName>

Operator entry points:

    quota_guard.py status [--json] [--refresh|--fresh]
    quota_guard.py plan --items 7
    quota_guard.py check [--milestone]
    quota_guard.py handoff --goal "..." [--slug ...]
    quota_guard.py selftest

Never prints or copies authentication tokens.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
# 状态目录固定在家目录，避免 Codex 桌面版（CODEX_HOME 指向 /mnt/c/...）与终端 CLI
# 各写各的 gate 文件，导致判定互相看不见。
STATE_DIR = Path(os.environ.get("QUOTA_GUARD_STATE") or (Path.home() / ".codex" / "quota-guard"))
GATE_PATH = STATE_DIR / "gate.json"
LEDGER_PATH = STATE_DIR / "ledger.jsonl"
LOG_PATH = STATE_DIR / "hook.log"

OK, TIGHT, CRITICAL, EXHAUSTED, WAIT_RESET, UNKNOWN = (
    "OK",
    "TIGHT",
    "CRITICAL",
    "EXHAUSTED",
    "WAIT_RESET",
    "UNKNOWN",
)
EXIT_CODES = {OK: 0, TIGHT: 10, CRITICAL: 11, EXHAUSTED: 12, WAIT_RESET: 13, UNKNOWN: 13}

# 测试用：QUOTA_GUARD_FORCE=EXHAUSTED 强制覆盖判定，不碰真额度
_FORCE: str | None = None
# 测试用：覆盖 auth.json 搜索路径与额度抓取函数，避免 selftest 读真实账号
_AUTH_PATHS: list[Path] | None = None
_FETCH: Any = None

DEFAULTS: dict[str, Any] = {
    "thresholds": {
        "exhausted_5h": 0.5,
        "critical_5h": 10.0,
        "critical_week": 5.0,
        "tight_5h": 15.0,
        "tight_week": 10.0,
        "reserve_for_handoff": 3.0,
    },
    "cadence": {
        "bash_calls_between_checks": 8,
        "model_requests_between_checks": 10,
        "cache_ttl_seconds": 60,
        "force_refresh_seconds": 300,
        "stop_hook_max_blocks": 2,
    },
    "estimate": {
        "turns_per_item": 1.5,
        "burn_ewma_alpha": 0.3,
        "default_burn_per_turn": 1.0,
        "handoff_fresh_seconds": 1800,
    },
    "wait_reset": {"enabled": False, "max_wait_minutes": 20},
    "handoff": {"dir": ".handoff", "exclude_mode": "info-exclude"},
    "expensive_patterns": [],
    "handoff_allow_patterns": [],
}


# --------------------------------------------------------------------------- config


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config() -> dict[str, Any]:
    cfg = deepcopy(DEFAULTS)
    path = SKILL_DIR / "config" / "thresholds.yaml"
    if not path.exists():
        return cfg
    try:
        import yaml  # type: ignore
    except Exception:  # pragma: no cover - yaml missing is fine, defaults hold
        return cfg
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict):
            _deep_merge(cfg, data)
    except Exception as exc:  # noqa: BLE001
        log(f"config load failed: {exc}")
    return cfg


def log(message: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- quota


def _find_rate_limits(node: Any, depth: int = 0) -> dict[str, Any] | None:
    if depth > 6:
        return None
    if isinstance(node, dict):
        candidate = node.get("rate_limits")
        if isinstance(candidate, dict) and ("primary" in candidate or "secondary" in candidate):
            return candidate
        for key in ("payload", "msg", "message", "event", "info"):
            if key in node:
                hit = _find_rate_limits(node[key], depth + 1)
                if hit:
                    return hit
    return None


def _parse_ts(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def tail_rate_limits_record(path: Path, start_window: int = 65536,
                            max_window: int = 4 * 1024 * 1024
                            ) -> tuple[dict[str, Any], float | None] | None:
    """Newest `rate_limits` object plus the timestamp of the record that carried it.

    The timestamp matters: a line written before an account switch describes the previous
    login's quota, and its shape is indistinguishable from a fresh one.
    """
    if not path or not path.exists():
        return None
    try:
        size = path.stat().st_size
        window = start_window
        while window <= max_window:
            with path.open("rb") as handle:
                handle.seek(max(0, size - window))
                chunk = handle.read().decode("utf-8", "ignore")
            lines = chunk.splitlines()
            if size > window:
                lines = lines[1:]  # first line is very likely truncated
            for line in reversed(lines):
                if '"rate_limits"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                hit = _find_rate_limits(record)
                if hit:
                    written_at = (_parse_ts(record.get("timestamp"))
                                  if isinstance(record, dict) else None)
                    return hit, written_at
            if window >= size:
                break
            window *= 4
    except OSError as exc:
        log(f"tail_rate_limits failed: {exc}")
    return None


def tail_rate_limits(path: Path, start_window: int = 65536, max_window: int = 4 * 1024 * 1024
                     ) -> dict[str, Any] | None:
    """Read the newest `rate_limits` object from a rollout JSONL without parsing the file."""
    found = tail_rate_limits_record(path, start_window, max_window)
    return found[0] if found else None


def session_roots() -> list[Path]:
    """Every CODEX_HOME that may hold rollouts: CLI home, desktop-app home, explicit env."""
    candidates: list[Path] = []
    env = os.environ.get("CODEX_HOME")
    if env:
        candidates.append(Path(env))
    candidates.append(Path.home() / ".codex")
    try:
        candidates.extend(Path("/mnt/c/Users").glob("*/.codex"))
    except OSError:
        pass

    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if (candidate / "sessions").is_dir():
            roots.append(candidate)
    return roots


# --------------------------------------------------------------------------- account


# 只缓存 account_id 与 mtime，绝不缓存或打印 token。
_AUTH_CACHE: dict[str, Any] = {"key": None, "account": None}

# rollout 里的 timestamp 常被截到整秒，切换瞬间写的行会看起来"早于"切换；
# 留 2 秒余量，避免把当前账号自己的第一行额度当成上一个账号的。
SWITCH_SLACK_SECONDS = 2.0


def auth_candidates() -> list[Path]:
    """Every auth.json that can hold the live login: explicit env, CLI home, desktop-app home."""
    if _AUTH_PATHS:
        return list(_AUTH_PATHS)
    candidates: list[Path] = []
    env = os.environ.get("CODEX_HOME")
    if env:
        candidates.append(Path(env) / "auth.json")
    candidates.append(Path.home() / ".codex" / "auth.json")
    try:
        candidates.extend(Path("/mnt/c/Users").glob("*/.codex/auth.json"))
    except OSError:
        pass

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def auth_state() -> tuple[str | None, float | None]:
    """(account id, auth.json mtime) of the most recently written auth.json.

    Switching accounts is done by rewriting auth.json, so the newest file wins.
    """
    best: Path | None = None
    best_mtime = 0.0
    for candidate in auth_candidates():
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = candidate, mtime
    if best is None:
        return None, None

    cache_key = f"{best}:{best_mtime}"
    if _AUTH_CACHE.get("key") == cache_key:
        return _AUTH_CACHE.get("account"), best_mtime

    account: str | None = None
    try:
        data = json.loads(best.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"auth_state unreadable: {type(exc).__name__}")
        data = None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if isinstance(tokens, dict):
        value = tokens.get("account_id")
        if isinstance(value, str) and value.strip():
            account = value.strip()
    _AUTH_CACHE["key"] = cache_key
    _AUTH_CACHE["account"] = account
    return account, best_mtime


def account_state(gate: dict[str, Any]) -> tuple[str | None, float | None, bool]:
    """(account id, not_before, changed).

    `not_before` is the moment the current account took over. Rate-limit lines written to a
    rollout before it belong to the previous login, however fresh they look.
    """
    account, auth_mtime = auth_state()
    known = gate.get("account")
    known = known if isinstance(known, dict) else {}
    previous = known.get("id")
    switched_at = known.get("switched_at")
    if not account:
        return previous, switched_at, False
    if previous and previous != account:
        return account, auth_mtime or time.time(), True
    return account, switched_at, False


def rollout_creator(path: Path) -> str | None:
    """creator_account_id from session_meta — stamped when the session started, not per line."""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for _ in range(5):
                line = handle.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload") if isinstance(record, dict) else None
                if not isinstance(payload, dict):
                    continue
                value = payload.get("creator_account_id")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    except OSError as exc:
        log(f"rollout_creator failed: {exc}")
    return None


def rollout_usable(path: Path, written_at: float | None, account: str | None,
                   not_before: float | None) -> bool:
    """A rollout may speak for `account` if it was written after the switch or created by it."""
    if not account:
        return True
    if (not_before and written_at is not None
            and written_at + SWITCH_SLACK_SECONDS >= float(not_before)):
        return True
    creator = rollout_creator(path)
    if creator is None:
        return True
    return creator == account


def short_account(account: Any) -> str:
    if not isinstance(account, str) or not account:
        return "未知"
    return account[:8]


def newest_rollout(account: str | None = None,
                   not_before: float | None = None) -> Path | None:
    best: Path | None = None
    best_mtime = 0.0
    today = datetime.now().astimezone()
    roots = session_roots()
    # 先看最近一天，全部 CODEX_HOME 一起比，再往前退，避免某个旧 home 抢答
    for offset in range(3):
        day = today - timedelta(days=offset)
        candidates: list[tuple[float, Path]] = []
        for root in roots:
            folder = root / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
            if not folder.is_dir():
                continue
            try:
                for candidate in folder.glob("*.jsonl"):
                    candidates.append((candidate.stat().st_mtime, candidate))
            except OSError:
                continue
        for mtime, candidate in sorted(candidates, key=lambda item: item[0], reverse=True):
            if best is not None and mtime < best_mtime:
                continue
            written_at = mtime
            # 文件可能横跨一次切换：旧账号开的会话被新账号接着写。这种时候文件 mtime
            # 说不清归属，只能看最新那行额度自己的时间戳。
            if account and not_before and mtime + SWITCH_SLACK_SECONDS >= float(not_before):
                found = tail_rate_limits_record(candidate)
                if found and found[1] is not None:
                    written_at = float(found[1])
            # 切换账号后，上一个账号的会话文件不能拿来回答当前账号的额度
            if not rollout_usable(candidate, written_at, account, not_before):
                continue
            best, best_mtime = candidate, mtime
        if best:
            return best
    return best


def normalize(raw: dict[str, Any], source: str, now: float) -> dict[str, Any]:
    def window(key: str) -> dict[str, Any] | None:
        item = raw.get(key)
        if not isinstance(item, dict):
            return None
        used = item.get("used_percent")
        if used is None:
            used = item.get("usedPercent")
        if not isinstance(used, (int, float)):
            return None
        resets = item.get("resets_at", item.get("resetsAt"))
        minutes = item.get("window_minutes", item.get("windowDurationMins"))
        if isinstance(resets, (int, float)) and resets < now:
            # window already rolled over; stale data would understate the new window
            return {
                "used": 0.0,
                "window_minutes": minutes,
                "resets_at": resets,
                "stale": True,
            }
        return {
            "used": float(used),
            "window_minutes": minutes,
            "resets_at": resets,
            "stale": False,
        }

    return {
        "source": source,
        "captured_at": now,
        "primary": window("primary"),
        "secondary": window("secondary"),
        "reached_type": raw.get("rate_limit_reached_type"),
        "plan_type": raw.get("plan_type"),
        "credits": raw.get("credits"),
        "account": raw.get("account"),
    }


def from_rollout(transcript: str | None = None, account: str | None = None,
                 not_before: float | None = None) -> dict[str, Any] | None:
    now = time.time()
    path = Path(transcript) if transcript else None
    if not path or not path.exists():
        path = newest_rollout(account, not_before)
    found = tail_rate_limits_record(path) if path else None
    if not found:
        return None
    raw, written_at = found
    # 切换账号后，上一个账号写的额度行不算数：等当前账号自己写，或者退回 app-server。
    if (not_before and written_at is not None
            and written_at + SWITCH_SLACK_SECONDS < float(not_before)):
        return None
    creator = rollout_creator(path)
    if account and creator and creator != account:
        post_switch = bool(not_before and written_at is not None
                           and written_at + SWITCH_SLACK_SECONDS >= float(not_before))
        if not post_switch:
            return None
    snapshot = normalize(raw, "rollout", now)
    snapshot["account"] = account or creator
    snapshot["written_at"] = written_at
    return snapshot


def from_app_server(timeout: float = 20.0) -> dict[str, Any] | None:
    helper = SKILL_DIR / "scripts" / "quota_status.py"
    if not helper.exists():
        return None
    try:
        proc = subprocess.run(
            [sys.executable or "python3", str(helper), "--json", "--timeout", str(timeout)],
            capture_output=True,
            text=True,
            timeout=timeout + 8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"app-server fallback failed: {exc}")
        return None
    if proc.returncode != 0:
        log(f"app-server fallback exit {proc.returncode}: {proc.stderr.strip()[:200]}")
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    buckets = payload.get("buckets") or []
    if not buckets:
        return None
    bucket = next((b for b in buckets if b.get("limitId") == "codex"), buckets[0])
    now = time.time()
    raw: dict[str, Any] = {
        "rate_limit_reached_type": bucket.get("rateLimitReachedType"),
        # app-server 直接回报当前登录账号，是切换后唯一权威的归属证据
        "account": payload.get("accountId"),
    }
    for window in bucket.get("windows") or []:
        minutes = window.get("windowDurationMins")
        key = "primary" if minutes == 300 else "secondary"
        if key in raw and key == "primary":
            continue
        raw[key] = {
            "used_percent": window.get("usedPercent"),
            "resets_at": window.get("resetsAt"),
            "window_minutes": minutes,
        }
    return normalize(raw, "app-server", now)


def fetch_quota(transcript: str | None, force_app_server: bool = False,
                account: str | None = None, not_before: float | None = None
                ) -> dict[str, Any] | None:
    if not force_app_server:
        snapshot = from_rollout(transcript, account, not_before)
        if snapshot and not _is_stale(snapshot, account):
            return snapshot
    return from_app_server() or from_rollout(transcript, account, not_before)


def _is_stale(snapshot: dict[str, Any], account: str | None = None) -> bool:
    primary = snapshot.get("primary") or {}
    if primary.get("stale"):
        return True
    owner = snapshot.get("account")
    if account and owner and owner != account:
        return True
    return False


def remaining(snapshot: dict[str, Any], key: str) -> float | None:
    window = snapshot.get(key) or {}
    used = window.get("used")
    if not isinstance(used, (int, float)):
        return None
    return max(0.0, min(100.0, 100.0 - float(used)))


# --------------------------------------------------------------------------- verdict


def evaluate(snapshot: dict[str, Any] | None, cfg: dict[str, Any], plan: dict[str, Any] | None
             ) -> dict[str, Any]:
    if not snapshot:
        return {"verdict": UNKNOWN, "reason": "拿不到额度数据", "r5": None, "r7": None}

    th = cfg["thresholds"]
    r5 = remaining(snapshot, "primary")
    r7 = remaining(snapshot, "secondary")
    reached = snapshot.get("reached_type")
    plan = plan or {}
    est_turns = plan.get("est_turns")
    burn = plan.get("burn5")
    reserve = float(th["reserve_for_handoff"])

    base = {"r5": r5, "r7": r7, "reached_type": reached, "est_turns": est_turns, "burn5": burn}

    if reached:
        return {**base, "verdict": EXHAUSTED, "code": "reached", "reason": f"Codex 报限额：{reached}"}

    if r5 is not None and r5 <= float(th["exhausted_5h"]):
        return {**base, "verdict": EXHAUSTED, "code": "empty", "reason": f"5h 剩余 {r5:.2f}%"}

    budget = None
    if r5 is not None:
        budget = r5 - reserve
        if isinstance(est_turns, (int, float)) and isinstance(burn, (int, float)) and est_turns > 0:
            budget -= float(burn) * float(est_turns)

    if r5 is not None and r5 <= float(th["critical_5h"]):
        return {**base, "verdict": CRITICAL, "code": "threshold", "reason": f"5h 剩余 {r5:.1f}%"}

    if r7 is not None and r7 <= float(th["critical_week"]):
        return {**base, "verdict": CRITICAL, "code": "threshold", "reason": f"周剩余 {r7:.1f}%"}

    if budget is not None and budget < 0:
        wait = _wait_window(snapshot, cfg, plan)
        if wait:
            return {**base, "verdict": WAIT_RESET, "code": "wait",
                    "reason": f"预计 {est_turns} 轮做完会超；{wait} 分钟后窗口重置"}
        return {**base, "verdict": CRITICAL, "code": "cannot-finish",
                "reason": f"按当前 burn {burn}%/轮、剩 {est_turns} 轮，预算不够（余 {budget:.1f}%）"}

    if r5 is not None and r5 <= float(th["tight_5h"]):
        return {**base, "verdict": TIGHT, "code": "threshold", "reason": f"5h 剩余 {r5:.1f}%"}
    if r7 is not None and r7 <= float(th["tight_week"]):
        return {**base, "verdict": TIGHT, "code": "threshold", "reason": f"周剩余 {r7:.1f}%"}

    return {**base, "verdict": OK, "code": "ok", "reason": "额度充足"}


def _wait_window(snapshot: dict[str, Any], cfg: dict[str, Any], plan: dict[str, Any]
                 ) -> int | None:
    wait_cfg = cfg["wait_reset"]
    if not wait_cfg.get("enabled"):
        return None
    primary = snapshot.get("primary") or {}
    resets_at = primary.get("resets_at")
    if not isinstance(resets_at, (int, float)):
        return None
    minutes = (resets_at - time.time()) / 60.0
    if minutes <= 0 or minutes > float(wait_cfg["max_wait_minutes"]):
        return None
    return int(minutes)


# --------------------------------------------------------------------------- gate


def blank_gate() -> dict[str, Any]:
    return {
        "schema": 1,
        "updated_at": 0,
        "session_id": None,
        "cwd": None,
        "transcript": None,
        "quota": None,
        "r5": None,
        "r7": None,
        "verdict": UNKNOWN,
        "reason": "",
        "counters": {"bash": 0, "model_requests": 0, "turns": 0, "stop_blocks": 0},
        "plan": {"items": None, "est_turns": None, "burn5": None},
        "last": {"p5": None, "turns": 0},
        "notified": {},
        "account": None,
        "account_stale": False,
    }


def read_gate() -> dict[str, Any]:
    try:
        if GATE_PATH.exists():
            data = json.loads(GATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("schema") == 1:
                return data
    except Exception as exc:  # noqa: BLE001
        log(f"read_gate failed: {exc}")
    return blank_gate()


def write_gate(gate: dict[str, Any]) -> None:
    gate["updated_at"] = time.time()
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=STATE_DIR, prefix=".gate-", delete=False
        )
        try:
            json.dump(gate, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, GATE_PATH)
    except Exception as exc:  # noqa: BLE001
        log(f"write_gate failed: {exc}")


def append_ledger(gate: dict[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": int(gate.get("updated_at") or time.time()),
            "r5": gate.get("r5"),
            "r7": gate.get("r7"),
            "verdict": gate.get("verdict"),
            "bash": (gate.get("counters") or {}).get("bash"),
            "requests": (gate.get("counters") or {}).get("model_requests"),
            "turns": (gate.get("counters") or {}).get("turns"),
        }
        with LEDGER_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        log(f"append_ledger failed: {exc}")


def update_burn(gate: dict[str, Any], cfg: dict[str, Any]) -> None:
    """Keep an EWMA of 5h-percentage burned per assistant turn."""
    quota = gate.get("quota") or {}
    primary = quota.get("primary") or {}
    p5 = primary.get("used")
    counters = gate.get("counters") or {}
    turns = counters.get("turns") or 0
    last = gate.get("last") or {}
    last_p5 = last.get("p5")
    last_turns = last.get("turns") or 0

    if isinstance(p5, (int, float)) and isinstance(last_p5, (int, float)):
        delta_p = float(p5) - float(last_p5)
        delta_t = turns - last_turns
        if delta_t > 0 and delta_p >= 0:
            sample = delta_p / delta_t
            alpha = float(cfg["estimate"]["burn_ewma_alpha"])
            current = (gate.get("plan") or {}).get("burn5")
            if isinstance(current, (int, float)):
                burn = alpha * sample + (1 - alpha) * float(current)
            else:
                burn = sample
            gate.setdefault("plan", {})["burn5"] = round(burn, 3)

    gate["last"] = {"p5": p5, "turns": turns}


def account_switch_result() -> dict[str, Any]:
    """上一个账号的额度不能用来判定当前任务能不能做完：宁可 UNKNOWN，也不误判。"""
    return {
        "verdict": UNKNOWN,
        "reason": "登录账号已切换，现有额度属于上一个账号，需重新读取",
        "r5": None,
        "r7": None,
    }


def refresh(gate: dict[str, Any], cfg: dict[str, Any], transcript: str | None,
            force_app_server: bool = False) -> dict[str, Any]:
    account, not_before, changed = account_state(gate)
    if changed:
        log(f"account switched to {short_account(account)} at {fmt_reset(not_before)}")
    fetcher = _FETCH or fetch_quota
    snapshot = fetcher(transcript, force_app_server=force_app_server,
                       account=account, not_before=not_before)
    if transcript:
        gate["transcript"] = transcript
    if snapshot:
        gate["quota"] = snapshot
    gate["account"] = {"id": account, "switched_at": not_before}
    quota = gate.get("quota") or {}
    owner = quota.get("account")
    # 只有切换点之前取到的快照才确定属于上一个账号；切换之后问到的数据即使归属对不上
    # （两个 home 各登一个号），也比一直 UNKNOWN 有用，只在状态里标注来源。
    captured = float(quota.get("captured_at") or 0)
    after_switch = bool(not_before) and captured >= float(not_before)
    gate["account_stale"] = bool(account and owner and owner != account and not after_switch)
    update_burn(gate, cfg)
    plan = gate.get("plan") or {}
    result = evaluate(gate.get("quota"), cfg, plan)
    if gate["account_stale"]:
        result = account_switch_result()
    if _FORCE:
        result = {**result, "verdict": _FORCE, "reason": f"强制注入 {_FORCE}（测试用）"}
    gate["verdict"] = result["verdict"]
    gate["reason"] = result["reason"]
    gate["r5"] = result["r5"]
    gate["r7"] = result["r7"]
    write_gate(gate)
    append_ledger(gate)
    return gate


def fresh_enough(gate: dict[str, Any], cfg: dict[str, Any]) -> bool:
    ttl = float(cfg["cadence"]["cache_ttl_seconds"])
    return (time.time() - float(gate.get("updated_at") or 0)) <= ttl


# --------------------------------------------------------------------------- rendering


def fmt_reset(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "未知"
    return datetime.fromtimestamp(value).astimezone().strftime("%m-%d %H:%M")


def guard_block(gate: dict[str, Any], cfg: dict[str, Any]) -> str:
    quota = gate.get("quota") or {}
    primary = quota.get("primary") or {}
    verdict = gate.get("verdict") or UNKNOWN
    r5 = gate.get("r5")
    r7 = gate.get("r7")
    handoff_dir = (gate.get("cwd") or ".") + "/" + cfg["handoff"]["dir"]
    account = (gate.get("account") or {}).get("id") or quota.get("account")

    source = f"来源 {quota.get('source')}；5h 重置 {fmt_reset(primary.get('resets_at'))}"
    if account:
        source += f"；账号 {short_account(account)}"
        owner = quota.get("account")
        if owner and owner != account and not gate.get("account_stale"):
            source += f"（额度来自 {short_account(owner)}）"
    lines = [
        f"[Codex 配额护栏] 5h 剩余 {_pct(r5)}，周剩余 {_pct(r7)}；判定 {verdict}（{gate.get('reason')}）",
        source,
    ]
    if gate.get("account_stale"):
        lines.append("注意：登录账号已切换，上面这组额度属于上一个账号，不要据此判断可行性；"
                     "下次检查会重新读取当前账号额度。")
    lines.append("规则（本会话强制）：")
    if verdict == EXHAUSTED:
        lines += [
            "- 额度已耗尽：不要再派发任何工具调用，不要开始新步骤。",
            f"- 立刻生成交接：python3 {SKILL_DIR}/scripts/quota_guard.py handoff --goal \"<一句话目标>\"",
            f"- 交接落在 {handoff_dir}/<utc>-<slug>/，完成后输出 6 行暂停摘要并停。",
        ]
    elif verdict == CRITICAL:
        lines += [
            "- 只做完当前这一个原子步骤，不扩范围、不顺带重构。",
            f"- 步骤完成后立即写交接（同上 handoff 命令），不要再开新任务。",
        ]
    elif verdict == TIGHT:
        lines += [
            "- 不许扩大范围：不做计划外的重构、不新增依赖、不顺手改无关文件。",
            "- 每个里程碑后跑 quota_guard.py check --milestone。",
        ]
    else:
        lines += [
            "- 多步任务开工前：quota_guard.py plan --items <待办项数>，让它判一次可行性。",
            "- 每个里程碑后：quota_guard.py check --milestone。",
        ]
    return "\n".join(lines)


def _pct(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "未知"
    return f"{value:.1f}%"


def _owner_note(gate: dict[str, Any], quota: dict[str, Any]) -> str:
    if gate.get("account_stale"):
        return "（额度仍是上一个账号的，待刷新）"
    owner = quota.get("account")
    current = (gate.get("account") or {}).get("id")
    if owner and current and owner != current:
        return f"（额度来自 {short_account(owner)}）"
    return ""


def human_status(gate: dict[str, Any], cfg: dict[str, Any]) -> str:
    quota = gate.get("quota") or {}
    primary = quota.get("primary") or {}
    secondary = quota.get("secondary") or {}
    plan = gate.get("plan") or {}
    counters = gate.get("counters") or {}
    return "\n".join(
        [
            f"判定: {gate.get('verdict')} — {gate.get('reason')}",
            f"5h: 剩余 {_pct(gate.get('r5'))}（已用 {primary.get('used')}%），重置 {fmt_reset(primary.get('resets_at'))}",
            f"周: 剩余 {_pct(gate.get('r7'))}（已用 {secondary.get('used')}%），重置 {fmt_reset(secondary.get('resets_at'))}",
            f"估算: burn {plan.get('burn5')}%/轮，待办 {plan.get('items')} 项 ≈ {plan.get('est_turns')} 轮",
            f"计数: Bash {counters.get('bash')}，模型请求 {counters.get('model_requests')}，轮次 {counters.get('turns')}",
            f"数据: {quota.get('source')}，{fmt_reset(quota.get('captured_at'))}",
            f"账号: {short_account((gate.get('account') or {}).get('id') or quota.get('account'))}"
            + _owner_note(gate, quota),
        ]
    )


# --------------------------------------------------------------------------- git + handoff


def run_git(args: list[str], cwd: Path, timeout: float = 8.0) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def repo_root(cwd: Path) -> Path:
    top = run_git(["rev-parse", "--show-toplevel"], cwd)
    return Path(top) if top else cwd


def ensure_excluded(root: Path, cfg: dict[str, Any]) -> str:
    """Keep handoff directories out of version control without touching tracked files."""
    folder = cfg["handoff"]["dir"].rstrip("/")
    if folder.startswith("./"):
        folder = folder[2:]
    pattern = folder + "/"
    git_dir = run_git(["rev-parse", "--git-dir"], root)
    if not git_dir:
        return "no-git"
    exclude_path = (Path(git_dir) if Path(git_dir).is_absolute() else root / git_dir) / "info" / "exclude"
    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        if pattern in {line.strip() for line in existing.splitlines()}:
            return str(exclude_path)
        with exclude_path.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(f"# codex quota guard handoff\n{pattern}\n")
        return str(exclude_path)
    except OSError as exc:
        log(f"ensure_excluded failed: {exc}")
        return "error"


def collect_repo_state(root: Path) -> dict[str, Any]:
    stash = run_git(["stash", "create"], root)
    return {
        "path": str(root),
        "branch": run_git(["rev-parse", "--abbrev-ref", "HEAD"], root) or None,
        "head": run_git(["rev-parse", "--short", "HEAD"], root) or None,
        "dirty": bool(run_git(["status", "--porcelain"], root)),
        "stash": stash or None,
        "status": run_git(["status", "--porcelain"], root).splitlines()[:40],
        "diffstat": run_git(["diff", "--stat"], root).splitlines()[:20],
    }


HANDOFF_TEMPLATE = """# 交接文档 · {slug}

> 由 codex-quota-guard 在额度不足时生成。接手方：先读「下一步第一动作」，再动手。

## 0. 元信息

| 项 | 值 |
|---|---|
| 交接 ID | `{handoff_id}` |
| 生成时间 | {created_at} |
| 生成原因 | {reason} |
| 源 agent / 模型 | codex / {model} |
| 会话 | `{session_id}` |
| 仓库 | `{repo_path}` |
| 分支 / HEAD | `{branch}` / `{head}` |
| 工作区 | {dirty_text} |
| WIP 引用 | `{stash}` |
| 额度快照 | 5h 剩 {r5}，周剩 {r7}（{verdict}） |

## 1. 目标与验收标准

<!-- TODO: 一句话目标 + 可执行的验收标准（命令或断言）。接手方不该需要猜测。 -->

## 2. 已完成

<!-- TODO: 一条一行，附证据：commit / 文件:行 / 测试输出片段。 -->

## 3. 未完成（按顺序）

<!-- TODO: 每条固定四段：
- [ ] 任务
      entry: path/to/file.py:120
      expect: 期望行为
      verify: 验证命令
-->

## 4. 关键决策与理由

<!-- TODO: 为什么不用另一个方案。防止接手方推翻重来。 -->

## 5. 坑（试过且失败的路）

<!-- TODO: 每条含「做了什么 / 报什么错 / 结论」。没有就写「无」。 -->

## 6. 环境与复现

```
{repro_commands}
```

## 7. 下一步第一动作

<!-- TODO: 5 分钟内能开始的具体动作，不许写「继续开发」。 -->

## 8. 待确认问题

<!-- TODO: 卡在用户决策上的点；没有就写「无」。 -->

## 9. Resume prompt（可直接粘给下一个 agent）

```
你在 {repo_path} 接手一个未完成任务。先完整读 {handoff_file}，
然后复述「目标 + 下一步第一动作」，确认无误再动手。
不要重做「已完成」里的内容；「未完成」按顺序推进，每完成一条更新该文件。
```

---

### 现场快照

```
git status --porcelain:
{status_block}

git diff --stat:
{diffstat_block}
```
"""


def write_handoff(cfg: dict[str, Any], gate: dict[str, Any], goal: str, slug: str,
                  reason: str | None = None, cwd: Path | None = None) -> Path:
    root = repo_root(cwd or Path(gate.get("cwd") or os.getcwd()))
    folder = root / cfg["handoff"]["dir"] / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{slug}"
    folder.mkdir(parents=True, exist_ok=True)

    state = collect_repo_state(root)
    quota = gate.get("quota") or {}
    handoff_id = folder.name
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    excluded = ensure_excluded(root, cfg)

    meta = {
        "id": handoff_id,
        "created_at": created_at,
        "status": "partial",
        "reason": reason or gate.get("reason"),
        "goal": goal,
        "source": {
            "agent": "codex",
            "model": os.environ.get("CODEX_MODEL") or None,
            "session_id": gate.get("session_id"),
            "transcript": gate.get("transcript"),
        },
        "quota": {
            "verdict": gate.get("verdict"),
            "r5": gate.get("r5"),
            "r7": gate.get("r7"),
            "primary": quota.get("primary"),
            "secondary": quota.get("secondary"),
            "source": quota.get("source"),
        },
        "repo": state,
        "git_exclude": excluded,
        "next_action": None,
        "todo": [],
    }
    (folder / "handoff.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    handoff_file = folder / "HANDOFF.md"
    body = HANDOFF_TEMPLATE.format(
        slug=slug,
        handoff_id=handoff_id,
        created_at=created_at,
        reason=meta["reason"],
        model=meta["source"]["model"] or "unknown",
        session_id=gate.get("session_id") or "unknown",
        repo_path=state["path"],
        branch=state["branch"],
        head=state["head"],
        dirty_text="有未提交改动" if state["dirty"] else "干净",
        stash=state["stash"] or "无",
        r5=_pct(gate.get("r5")),
        r7=_pct(gate.get("r7")),
        verdict=gate.get("verdict"),
        repro_commands=f"cd {state['path']}\ngit status\n# TODO: 装上依赖、跑测试的命令",
        handoff_file=handoff_file,
        status_block="\n".join(state["status"]) or "(clean)",
        diffstat_block="\n".join(state["diffstat"]) or "(no diff)",
    )
    handoff_file.write_text(body, encoding="utf-8")

    gate.setdefault("handoff", {})
    gate["handoff"] = {"path": str(handoff_file), "id": handoff_id, "at": time.time()}
    write_gate(gate)
    return handoff_file


def latest_handoff(cwd: Path, cfg: dict[str, Any], session_id: str | None = None
                   ) -> tuple[Path | None, float]:
    folder = repo_root(cwd) / cfg["handoff"]["dir"]
    if not folder.exists():
        return None, 0.0
    newest, newest_mtime = None, 0.0
    try:
        for candidate in folder.glob("*/handoff.json"):
            if session_id:
                try:
                    meta = json.loads(candidate.read_text(encoding="utf-8"))
                    if ((meta.get("source") or {}).get("session_id")) != session_id:
                        continue
                except (OSError, json.JSONDecodeError):
                    continue
            handoff_file = candidate.parent / "HANDOFF.md"
            if not handoff_file.exists():
                continue
            mtime = max(candidate.stat().st_mtime, handoff_file.stat().st_mtime)
            if mtime > newest_mtime:
                newest, newest_mtime = handoff_file, mtime
    except OSError:
        pass
    return newest, newest_mtime


def handoff_complete(path: Path | None) -> bool:
    """A fresh scaffold is not a completed handoff until every TODO is replaced."""
    if not path or not path.exists():
        return False
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return False
    required = [f"## {index}." for index in range(10)]
    return "<!-- TODO:" not in body and all(section in body for section in required)


def ensure_handoff_draft(gate: dict[str, Any], cfg: dict[str, Any], cwd: Path) -> Path:
    """Create a recoverable scaffold before the remaining quota can reach zero."""
    existing, _ = latest_handoff(cwd, cfg, gate.get("session_id"))
    if existing:
        gate["handoff"] = {
            "path": str(existing),
            "id": existing.parent.name,
            "at": existing.stat().st_mtime,
        }
        write_gate(gate)
        return existing
    return write_handoff(
        cfg,
        gate,
        "完成当前 Codex 会话中尚未结束的用户任务",
        "quota-critical",
        reason=f"自动草拟：{gate.get('reason')}",
        cwd=cwd,
    )


# --------------------------------------------------------------------------- hooks


def emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def emit_context(event: str, text: str) -> int:
    return emit({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})


def emit_deny(event: str, reason: str) -> int:
    return emit(
        {
            "hookSpecificOutput": {
                "hookEventName": event,
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def matches(patterns: list[str], text: str) -> bool:
    for pattern in patterns or []:
        try:
            if re.search(pattern, text):
                return True
        except re.error:
            continue
    return False


def handoff_patch_only(command: str, cwd: Path, cfg: dict[str, Any]) -> bool:
    """Allow apply_patch only when it updates HANDOFF.md inside this repo's handoff dir."""
    if re.search(r"^\*\*\* (?:Add|Delete) File:|^\*\*\* Move to:", command, re.MULTILINE):
        return False
    targets = re.findall(r"^\*\*\* Update File:\s*(.+?)\s*$", command, re.MULTILINE)
    if not targets:
        return False

    root = repo_root(cwd).resolve()
    handoff_root = (root / cfg["handoff"]["dir"]).resolve()
    for raw in targets:
        target = Path(raw)
        candidates = [target.resolve()] if target.is_absolute() else [
            (cwd / target).resolve(),
            (root / target).resolve(),
        ]
        if not any(
            candidate.name == "HANDOFF.md" and candidate.is_relative_to(handoff_root)
            for candidate in candidates
        ):
            return False
    return True


def handoff_tool_allowed(tool_name: str, tool_input: Any, cwd: Path,
                         cfg: dict[str, Any]) -> bool:
    data = tool_input if isinstance(tool_input, dict) else {}
    command = str(data.get("command") or "")
    lowered = tool_name.lower()
    if lowered in ("bash", "shell", "local_shell", "exec"):
        return bool(command and matches(cfg["handoff_allow_patterns"], command))
    if lowered == "apply_patch":
        return handoff_patch_only(command, cwd, cfg)
    return False


def h_session_start(gate: dict[str, Any], payload: dict[str, Any], cfg: dict[str, Any]) -> int:
    refresh(gate, cfg, payload.get("transcript_path"), force_app_server=False)
    return emit_context("SessionStart", guard_block(gate, cfg))


def h_user_prompt(gate: dict[str, Any], payload: dict[str, Any], cfg: dict[str, Any]) -> int:
    # 切换账号必须立刻重读，不能等 cache_ttl 过期，否则这一轮还在用上一个账号的额度。
    _account, _not_before, changed = account_state(gate)
    if changed or not fresh_enough(gate, cfg):
        refresh(gate, cfg, payload.get("transcript_path"), force_app_server=changed)
    verdict = gate.get("verdict")
    if verdict in (EXHAUSTED, CRITICAL, WAIT_RESET):
        key = f"{verdict}:{gate.get('r5') is not None and round(float(gate['r5']))}"
        if gate.setdefault("notified", {}).get(key):
            return 0
        gate["notified"][key] = time.time()
        write_gate(gate)
        return emit_context("UserPromptSubmit", guard_block(gate, cfg))
    return 0


def h_pre_tool_use(gate: dict[str, Any], payload: dict[str, Any], cfg: dict[str, Any]) -> int:
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    command = str(tool_input.get("command") or "").strip() if isinstance(tool_input, dict) else ""
    cwd = Path(payload.get("cwd") or gate.get("cwd") or os.getcwd())
    age = time.time() - float(gate.get("updated_at") or 0)
    verdict = gate.get("verdict")

    # Once the guard is TIGHT, the rollout tail is cheap enough to refresh before every
    # supported tool. This closes the race where a long-context request burns several
    # percentage points between the asynchronous PostModelRequest samples.
    _account, _not_before, changed = account_state(gate)
    ttl = float(cfg["cadence"]["cache_ttl_seconds"])
    if changed or verdict in (TIGHT, CRITICAL, EXHAUSTED) or age > ttl:
        refresh(gate, cfg, payload.get("transcript_path"), force_app_server=changed)

    expensive = bool(command and matches(cfg["expensive_patterns"], command))
    age = time.time() - float(gate.get("updated_at") or 0)
    if expensive and age > float(cfg["cadence"]["force_refresh_seconds"]):
        refresh(gate, cfg, payload.get("transcript_path"), force_app_server=True)

    verdict = gate.get("verdict")
    if verdict in (CRITICAL, EXHAUSTED):
        creating_handoff = bool(re.search(r"quota_guard\.py\s+handoff\b", command))
        handoff_path: Path | None = None
        if not creating_handoff:
            handoff_path = ensure_handoff_draft(gate, cfg, cwd)
        else:
            existing, _ = latest_handoff(cwd, cfg, gate.get("session_id"))
            handoff_path = existing

        reason = (
            f"配额已进入 {verdict}（{gate.get('reason')}）。当前任务必须停止，"
            "只允许采集现场、生成交接和编辑交接文档；不要继续实现或验证。"
        )
        if handoff_path:
            reason += f" 已自动生成交接草稿：{handoff_path}；先填完其中全部 TODO。"

        if handoff_tool_allowed(tool_name, tool_input, cwd, cfg):
            key = f"pretool-handoff:{verdict}:{gate.get('session_id')}"
            if not gate.setdefault("notified", {}).get(key):
                gate["notified"][key] = time.time()
                write_gate(gate)
                return emit_context("PreToolUse", reason)
            return 0
        return emit_deny(
            "PreToolUse",
            reason,
        )
    return 0


def h_post_tool_use(gate: dict[str, Any], payload: dict[str, Any], cfg: dict[str, Any]) -> int:
    counters = gate.setdefault("counters", {})
    counters["bash"] = int(counters.get("bash") or 0) + 1
    every = int(cfg["cadence"]["bash_calls_between_checks"])
    if every > 0 and counters["bash"] % every == 0:
        refresh(gate, cfg, payload.get("transcript_path"))
    else:
        write_gate(gate)
    return 0


def h_post_model_request(gate: dict[str, Any], payload: dict[str, Any], cfg: dict[str, Any]) -> int:
    counters = gate.setdefault("counters", {})
    counters["model_requests"] = int(counters.get("model_requests") or 0) + 1
    usage = payload.get("token_usage") or {}
    if isinstance(usage, dict) and usage.get("total_tokens"):
        gate["tokens"] = int(gate.get("tokens") or 0) + int(usage["total_tokens"])
    every = int(cfg["cadence"]["model_requests_between_checks"])
    if every > 0 and counters["model_requests"] % every == 0:
        refresh(gate, cfg, payload.get("transcript_path"))
    else:
        write_gate(gate)
    return 0


def h_pre_compact(gate: dict[str, Any], payload: dict[str, Any], cfg: dict[str, Any]) -> int:
    gate["compact_at"] = time.time()
    write_gate(gate)
    return 0


def h_stop(gate: dict[str, Any], payload: dict[str, Any], cfg: dict[str, Any]) -> int:
    counters = gate.setdefault("counters", {})
    counters["turns"] = int(counters.get("turns") or 0) + 1
    refresh(gate, cfg, payload.get("transcript_path"))

    verdict = gate.get("verdict")
    if verdict not in (CRITICAL, EXHAUSTED):
        gate["counters"]["stop_blocks"] = 0
        write_gate(gate)
        return 0

    cwd = Path(payload.get("cwd") or gate.get("cwd") or os.getcwd())
    handoff_file, mtime = latest_handoff(cwd, cfg, gate.get("session_id"))
    fresh_window = float(cfg["estimate"]["handoff_fresh_seconds"])
    if mtime and (time.time() - mtime) <= fresh_window and handoff_complete(handoff_file):
        return emit({"systemMessage": f"配额 {verdict}；交接文档已就绪：{handoff_file}"})

    if payload.get("stop_hook_active"):
        return 0
    if int(counters.get("stop_blocks") or 0) >= int(cfg["cadence"]["stop_hook_max_blocks"]):
        return 0

    counters["stop_blocks"] = int(counters.get("stop_blocks") or 0) + 1
    write_gate(gate)
    return emit(
        {
            "decision": "block",
            "reason": (
                f"配额 {verdict}（{gate.get('reason')}），本轮还没有交接文档。结束前必须："
                f"1) 跑 python3 {SKILL_DIR}/scripts/quota_guard.py handoff --goal \"<一句话目标>\"；"
                f"2) 把 HANDOFF.md 里 9 段 TODO 填成可执行内容（只有草稿也算未完成）；"
                f"3) 输出 6 行暂停摘要（原因/剩余额度/交接路径/建议接手/下一步第一动作/resume 命令）。"
                "不要求你完成剩余工作，只要求交接写清楚。"
            ),
        }
    )


HOOKS = {
    "SessionStart": h_session_start,
    "UserPromptSubmit": h_user_prompt,
    "PreToolUse": h_pre_tool_use,
    "PostToolUse": h_post_tool_use,
    # PostModelRequest 只有新版 codex 才有：standalone 0.154.0 里没有这个字符串，
    # 但桌面版（0.167 WSL 桥）和 VSCode 扩展（0.169）都会触发它，且它们共用同一个
    # CODEX_HOME，所以这个 home 通常能收到。纯 standalone 收不到时也不影响护栏——
    # PreToolUse（每 10 次 Bash，TIGHT 时每次都刷）和 Stop 已经兜住。
    "PostModelRequest": h_post_model_request,
    "PreCompact": h_pre_compact,
    "Stop": h_stop,
}


def run_hook(event: str) -> int:
    global _FORCE
    _FORCE = os.environ.get("QUOTA_GUARD_FORCE") or None
    if os.environ.get("QUOTA_GUARD_DISABLE") == "1":
        return 0
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}

    cfg = load_config()
    gate = read_gate()
    try:
        handler = HOOKS.get(event)
        if not handler:
            return 0
        if payload.get("session_id"):
            gate["session_id"] = payload["session_id"]
        if payload.get("cwd"):
            gate["cwd"] = payload["cwd"]
        if _FORCE:
            gate["verdict"] = _FORCE
            gate["reason"] = f"强制注入 {_FORCE}（测试用）"
        code = handler(gate, payload, cfg)
        blocked = code == 0 and gate.get("verdict") in (EXHAUSTED, CRITICAL)
        log(f"{event} verdict={gate.get('verdict')} r5={gate.get('r5')} blocked={blocked}")
        return code
    except Exception as exc:  # noqa: BLE001
        log(f"hook {event} failed: {exc!r}")
        return 0  # fail-open: never let the guard block real work


# --------------------------------------------------------------------------- cli


def cmd_hook(args: argparse.Namespace) -> int:
    return run_hook(args.event)


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    gate = read_gate()
    # 切了账号还读缓存，等于把上一个账号的数字当成当前的：先认账号再决定要不要重读。
    _account, _not_before, changed = account_state(gate)
    if args.refresh or args.fresh or changed or not gate.get("quota"):
        refresh(gate, cfg, None, force_app_server=changed or args.fresh)
    if args.json:
        print(json.dumps(
            {
                "verdict": gate.get("verdict"),
                "reason": gate.get("reason"),
                "r5": gate.get("r5"),
                "r7": gate.get("r7"),
                "quota": gate.get("quota"),
                "account": gate.get("account"),
                "account_stale": gate.get("account_stale"),
                "plan": gate.get("plan"),
                "counters": gate.get("counters"),
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print(human_status(gate, cfg))
    return EXIT_CODES.get(gate.get("verdict") or UNKNOWN, 13)


def cmd_plan(args: argparse.Namespace) -> int:
    cfg = load_config()
    gate = read_gate()
    gate.setdefault("plan", {})["items"] = args.items
    gate["plan"]["est_turns"] = round(args.items * float(cfg["estimate"]["turns_per_item"]), 1)
    refresh(gate, cfg, None, force_app_server=args.fresh)
    print(human_status(gate, cfg))
    return EXIT_CODES.get(gate.get("verdict") or UNKNOWN, 13)


def cmd_check(args: argparse.Namespace) -> int:
    cfg = load_config()
    gate = read_gate()
    if args.milestone:
        gate.setdefault("counters", {})["milestones"] = int(
            (gate.get("counters") or {}).get("milestones") or 0
        ) + 1
    refresh(gate, cfg, None)
    print(human_status(gate, cfg))
    return EXIT_CODES.get(gate.get("verdict") or UNKNOWN, 13)


def cmd_handoff(args: argparse.Namespace) -> int:
    cfg = load_config()
    gate = read_gate()
    if not gate.get("quota"):
        refresh(gate, cfg, None)
    slug = args.slug or re.sub(r"[^a-z0-9]+", "-", args.goal.lower())[:32].strip("-") or "task"
    path = write_handoff(cfg, gate, args.goal, slug, reason=args.reason,
                         cwd=Path(args.cwd) if args.cwd else None)
    print(str(path))
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    return selftest()


def selftest() -> int:
    global STATE_DIR, GATE_PATH, LEDGER_PATH, LOG_PATH, _FORCE, _AUTH_PATHS, _FETCH

    cfg = load_config()
    failures: list[str] = []

    # never touch the real state directory while testing
    sandbox = tempfile.TemporaryDirectory()
    original = (STATE_DIR, GATE_PATH, LEDGER_PATH, LOG_PATH, _FORCE, _AUTH_PATHS, _FETCH)
    STATE_DIR = Path(sandbox.name) / "quota-guard"
    GATE_PATH = STATE_DIR / "gate.json"
    LEDGER_PATH = STATE_DIR / "ledger.jsonl"
    LOG_PATH = STATE_DIR / "hook.log"
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    def check(name: str, actual: Any, expected: Any) -> None:
        if actual != expected:
            failures.append(f"{name}: got {actual!r}, want {expected!r}")

    now = time.time()
    future = now + 3600

    def snap(used5: float, used7: float = 10.0, reached: Any = None,
             resets: float | None = None) -> dict[str, Any]:
        return {
            "source": "test",
            "captured_at": now,
            "primary": {"used": used5, "window_minutes": 300,
                        "resets_at": resets if resets is not None else future, "stale": False},
            "secondary": {"used": used7, "window_minutes": 10080,
                          "resets_at": future + 86400, "stale": False},
            "reached_type": reached,
            "plan_type": "plus",
            "credits": None,
        }

    check("ok", evaluate(snap(10), cfg, None)["verdict"], OK)
    check("tight", evaluate(snap(88), cfg, None)["verdict"], TIGHT)
    check("critical@90", evaluate(snap(90), cfg, None)["verdict"], CRITICAL)
    check("critical@96", evaluate(snap(96), cfg, None)["verdict"], CRITICAL)
    check("critical@96.5", evaluate(snap(96.5), cfg, None)["verdict"], CRITICAL)
    check("exhausted@100", evaluate(snap(100), cfg, None)["verdict"], EXHAUSTED)
    check("reached", evaluate(snap(40, reached="primary"), cfg, None)["verdict"], EXHAUSTED)
    check("week-critical", evaluate(snap(20, 96), cfg, None)["verdict"], CRITICAL)
    check(
        "cannot-finish",
        evaluate(snap(50), cfg, {"est_turns": 60, "burn5": 1.0})["verdict"],
        CRITICAL,
    )
    check(
        "unknown",
        evaluate(None, cfg, None)["verdict"],
        UNKNOWN,
    )

    # stale window must read as 0 used
    stale = normalize(
        {"primary": {"used_percent": 90, "resets_at": now - 10, "window_minutes": 300}},
        "test",
        now,
    )
    check("stale-primaries-reset", stale["primary"]["used"], 0.0)

    # rollout tail parsing
    with tempfile.TemporaryDirectory() as tmp:
        rollout = Path(tmp) / "rollout.jsonl"
        records = [
            {"type": "session_meta", "payload": {"id": "x"}},
            {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": 42.0, "window_minutes": 300, "resets_at": future},
                "secondary": {"used_percent": 11.0, "window_minutes": 10080,
                              "resets_at": future + 86400},
            }}},
        ]
        rollout.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        raw = tail_rate_limits(rollout)
        check("tail-parse", bool(raw), True)
        check("tail-used", raw["primary"]["used_percent"] if raw else None, 42.0)

        # single-line file with no trailing newline, plus junk prefix
        rollout.write_text('{"broken\n' + json.dumps(records[1]), encoding="utf-8")
        check("tail-parse-partial-line", bool(tail_rate_limits(rollout)), True)

        # ---- account provenance: a rollout line written by the previous login must not count
        check("parse-ts-iso", _parse_ts("1970-01-01T00:00:10Z"), 10.0)
        check("parse-ts-number", _parse_ts(12.5), 12.5)
        check("parse-ts-garbage", _parse_ts("not-a-time"), None)

        def rollout_file(name: str, creator: Any, written_at: float,
                         used: float = 50.0) -> Path:
            path = Path(tmp) / name
            stamp = datetime.fromtimestamp(written_at, timezone.utc).isoformat()
            rows: list[dict[str, Any]] = []
            if creator:
                rows.append({"type": "session_meta",
                             "payload": {"id": name, "creator_account_id": creator}})
            rows.append({"type": "event_msg", "timestamp": stamp,
                         "payload": {"type": "token_count", "rate_limits": {
                             "limit_id": "codex",
                             "primary": {"used_percent": used, "window_minutes": 300,
                                         "resets_at": future},
                             "secondary": {"used_percent": 10.0, "window_minutes": 10080,
                                           "resets_at": future + 86400}}}})
            path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            return path

        old_line = rollout_file("old-account.jsonl", "acct-old", now - 600, used=91.0)
        new_line = rollout_file("new-account.jsonl", "acct-new", now - 10, used=5.0)
        anon_line = rollout_file("anon.jsonl", None, now - 10, used=7.0)

        check("creator-read", rollout_creator(old_line), "acct-old")
        record = tail_rate_limits_record(old_line)
        check("record-has-timestamp", bool(record and record[1]), True)
        check("record-used", record[0]["primary"]["used_percent"] if record else None, 91.0)

        # 切换前写的行（时间戳早于切换点）直接不认，哪怕它看起来是新的
        check("pre-switch-line-rejected",
              from_rollout(str(old_line), "acct-new", now - 60), None)
        accepted = (from_rollout(str(new_line), "acct-new", now - 60) or {}).get("primary") or {}
        check("post-switch-line-accepted", accepted.get("used"), 5.0)
        # 没有切换点时退回 creator 归属判断
        check("creator-mismatch-rejected", from_rollout(str(old_line), "acct-new", None), None)
        check("creator-match-accepted",
              (from_rollout(str(old_line), "acct-old", None) or {}).get("primary", {}).get("used"),
              91.0)
        check("creator-absent-allowed",
              bool(from_rollout(str(anon_line), "acct-new", None)), True)
        check("usable-before-switch", rollout_usable(old_line, now - 600, "acct-new", now - 60),
              False)
        check("usable-post-switch", rollout_usable(old_line, now - 10, "acct-new", now - 60),
              True)
        check("stale-on-account-mismatch",
              _is_stale({"primary": {"stale": False}, "account": "acct-old"}, "acct-new"), True)
        check("fresh-when-account-matches",
              _is_stale({"primary": {"stale": False}, "account": "acct-new"}, "acct-new"), False)
        # 时间戳常被截到整秒，切换瞬间写的行不能因为差半秒就被判成上一个账号的
        check("usable-slack", rollout_usable(old_line, now - 61, "acct-new", now - 60), True)

        # ---- newest_rollout: 选文件不能只看 mtime
        switch_at = now - 60
        sessions = Path(tmp) / "codex-sessions"
        day = sessions / "sessions" / f"{datetime.now():%Y}" / f"{datetime.now():%m}" \
            / f"{datetime.now():%d}"
        day.mkdir(parents=True)

        def session_file(name: str, creator: Any, line_at: float, mtime: float,
                         used: float = 50.0) -> Path:
            path = day / name
            stamp = datetime.fromtimestamp(line_at, timezone.utc).isoformat()
            rows: list[dict[str, Any]] = []
            if creator:
                rows.append({"type": "session_meta",
                             "payload": {"id": name, "creator_account_id": creator}})
            rows.append({"type": "event_msg", "timestamp": stamp,
                         "payload": {"type": "token_count", "rate_limits": {
                             "limit_id": "codex",
                             "primary": {"used_percent": used, "window_minutes": 300,
                                         "resets_at": future},
                             "secondary": {"used_percent": 10.0, "window_minutes": 10080,
                                           "resets_at": future + 86400}}}})
            path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            os.utime(path, (mtime, mtime))
            return path

        original_roots = globals()["session_roots"]
        globals()["session_roots"] = lambda: [sessions]
        try:
            mine = session_file("mine.jsonl", "acct-new", now - 10, now - 10, used=5.0)
            # 上一个账号的旧会话，文件刚被动过（比如追加了日志行），但额度行仍是切换前的
            touched = session_file("touched.jsonl", "acct-old", now - 900, now, used=91.0)
            check("newest-rollout-skips-stale-line", newest_rollout("acct-new", switch_at), mine)
            check("newest-rollout-unfiltered", newest_rollout(None, None), touched)

            # 旧账号开的会话被新账号接着写：文件 mtime 新，额度行也新 → 可以用
            mine.unlink()
            mixed = session_file("mixed.jsonl", "acct-old", now - 10, now - 5, used=7.0)
            check("newest-rollout-accepts-cross-switch", newest_rollout("acct-new", switch_at),
                  mixed)
        finally:
            globals()["session_roots"] = original_roots

        # ---- switch detection through auth.json
        auth = Path(tmp) / "auth.json"

        def set_auth(account: str, written_at: float) -> None:
            auth.write_text(json.dumps({"tokens": {"account_id": account,
                                                   "access_token": "SECRET"}}),
                            encoding="utf-8")
            os.utime(auth, (written_at, written_at))
            _AUTH_CACHE.update({"key": None, "account": None})

        _AUTH_PATHS = [auth]
        set_auth("acct-old", now - 900)
        check("auth-account", auth_state()[0], "acct-old")

        gate = blank_gate()
        gate["account"] = {"id": "acct-old", "switched_at": None}
        set_auth("acct-new", now - 120)
        account, not_before, changed = account_state(gate)
        check("switch-detected", changed, True)
        check("switch-account", account, "acct-new")
        check("switch-not-before", not_before, now - 120)

        # 上一次账号的额度仍在 gate 里时，判定必须降级为 UNKNOWN 并给出原因
        stale_snapshot = snap(50)
        stale_snapshot["account"] = "acct-old"
        stale_snapshot["captured_at"] = now - 180  # 切换之前取到的，确定是上一个账号的
        _FETCH = lambda *a, **k: stale_snapshot  # noqa: E731
        refresh(gate, cfg, None)
        check("switch-account-stale", gate.get("account_stale"), True)
        check("switch-verdict-unknown", gate.get("verdict"), UNKNOWN)
        check("switch-reason", "切换" in str(gate.get("reason")), True)
        check("switch-gate-account", (gate.get("account") or {}).get("id"), "acct-new")
        check("switch-block-warns", "上一个账号" in guard_block(gate, cfg), True)
        check("switch-status-warns", "上一个账号" in human_status(gate, cfg), True)

        # 读到当前账号的额度后恢复正常判定
        fresh_snapshot = snap(50)
        fresh_snapshot["account"] = "acct-new"
        _FETCH = lambda *a, **k: fresh_snapshot  # noqa: E731
        refresh(gate, cfg, None)
        check("switch-cleared", gate.get("account_stale"), False)
        check("switch-verdict-ok", gate.get("verdict"), OK)
        check("switch-block-account", "acct-new" in guard_block(gate, cfg), True)

        # 切换之后才取到的数据不算"上一个账号的"：两个 home 各登一个号时，
        # 一直 UNKNOWN 会让护栏失效，不如用新数据并在状态里标注来源。
        other_snapshot = snap(50)
        other_snapshot["account"] = "acct-old"
        other_snapshot["captured_at"] = now + 5
        _FETCH = lambda *a, **k: other_snapshot  # noqa: E731
        refresh(gate, cfg, None)
        check("post-switch-owner-not-stale", gate.get("account_stale"), False)
        check("post-switch-owner-note", "额度来自 acct-old" in human_status(gate, cfg), True)
        check("post-switch-block-owner-note", "额度来自 acct-old" in guard_block(gate, cfg), True)
        _FETCH = None
        _AUTH_PATHS = None
        _AUTH_CACHE.update({"key": None, "account": None})

    # gate round-trip + handoff scaffold in a throwaway git repo
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        run_git(["init", "-q"], root)
        (root / "a.txt").write_text("hello\n", encoding="utf-8")
        run_git(["-c", "user.email=t@t", "-c", "user.name=t", "add", "."], root)
        run_git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], root)

        gate = blank_gate()
        gate["quota"] = snap(96)
        gate["verdict"] = CRITICAL
        gate["reason"] = "test"
        gate["session_id"] = "test-session"
        path = write_handoff(cfg, gate, "把 A 改成 B", "selftest", cwd=root)
        check("handoff-exists", path.exists(), True)
        body = path.read_text(encoding="utf-8")
        sections = sum(1 for line in body.splitlines() if line.startswith("## "))
        check("handoff-has-9-sections", sections, 10)
        check("handoff-json", (path.parent / "handoff.json").exists(), True)
        check("handoff-draft-incomplete", handoff_complete(path), False)
        exclude = (root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        check("excluded", ".handoff/" in exclude, True)
        check("handoff-ignored", run_git(["status", "--porcelain"], root), "")

        good_patch = f"*** Begin Patch\n*** Update File: {path}\n@@\n-old\n+new\n*** End Patch"
        bad_patch = "*** Begin Patch\n*** Update File: a.txt\n@@\n-old\n+new\n*** End Patch"
        check("handoff-patch-allowed", handoff_patch_only(good_patch, root, cfg), True)
        check("source-patch-blocked", handoff_patch_only(bad_patch, root, cfg), False)
        check(
            "handoff-bash-allowed",
            handoff_tool_allowed("Bash", {"command": "git status --porcelain"}, root, cfg),
            True,
        )
        check(
            "ordinary-bash-blocked",
            handoff_tool_allowed("Bash", {"command": "pnpm build"}, root, cfg),
            False,
        )

        completed = re.sub(r"<!-- TODO:.*?-->", "已填写", body, flags=re.DOTALL)
        path.write_text(completed, encoding="utf-8")
        check("handoff-filled-complete", handoff_complete(path), True)
        latest, _ = latest_handoff(root, cfg, "test-session")
        check("latest-current-session", latest, path)
        other, _ = latest_handoff(root, cfg, "other-session")
        check("latest-other-session", other, None)

        # End-to-end PreToolUse policy: force a handoff draft, deny ordinary work,
        # and still allow editing that exact draft.
        hook_rollout = root / "hook-rollout.jsonl"
        hook_rollout.write_text(json.dumps({
            "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": 90.0, "window_minutes": 300,
                            "resets_at": future},
                "secondary": {"used_percent": 10.0, "window_minutes": 10080,
                              "resets_at": future + 86400},
            }},
        }) + "\n", encoding="utf-8")
        hook_gate = blank_gate()
        hook_gate.update({"session_id": "hook-session", "cwd": str(root)})
        _FORCE = CRITICAL
        denied = io.StringIO()
        with redirect_stdout(denied):
            h_pre_tool_use(hook_gate, {
                "tool_name": "Bash",
                "tool_input": {"command": "pnpm build"},
                "cwd": str(root),
                "transcript_path": str(hook_rollout),
            }, cfg)
        denied_payload = json.loads(denied.getvalue())
        check(
            "critical-denies-ordinary-tool",
            ((denied_payload.get("hookSpecificOutput") or {}).get("permissionDecision")),
            "deny",
        )
        draft, _ = latest_handoff(root, cfg, "hook-session")
        check("critical-auto-draft", bool(draft and draft.exists()), True)

        allowed = io.StringIO()
        edit_command = (
            f"*** Begin Patch\n*** Update File: {draft}\n@@\n-old\n+new\n*** End Patch"
        )
        with redirect_stdout(allowed):
            h_pre_tool_use(hook_gate, {
                "tool_name": "apply_patch",
                "tool_input": {"command": edit_command},
                "cwd": str(root),
                "transcript_path": str(hook_rollout),
            }, cfg)
        allowed_text = allowed.getvalue().strip()
        allowed_payload = json.loads(allowed_text) if allowed_text else {}
        check(
            "critical-allows-handoff-edit",
            ((allowed_payload.get("hookSpecificOutput") or {}).get("permissionDecision")),
            None,
        )
        _FORCE = None

    STATE_DIR, GATE_PATH, LEDGER_PATH, LOG_PATH, _FORCE, _AUTH_PATHS, _FETCH = original
    sandbox.cleanup()

    if failures:
        print("SELFTEST FAILED")
        for item in failures:
            print(" -", item)
        return 1
    print("SELFTEST OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    hook = sub.add_parser("hook", help="hook 入口，Codex 通过 stdin 传 payload")
    hook.add_argument("event", choices=sorted(HOOKS))
    hook.set_defaults(func=cmd_hook)

    status = sub.add_parser("status", help="查看当前判定")
    status.add_argument("--json", action="store_true")
    status.add_argument("--refresh", action="store_true", help="强制重新取额度")
    status.add_argument("--fresh", action="store_true", help="直接问 app-server，不读 rollout")
    status.set_defaults(func=cmd_status)

    plan = sub.add_parser("plan", help="声明本次任务的待办项数，判一次可行性")
    plan.add_argument("--items", type=int, required=True)
    plan.add_argument("--fresh", action="store_true")
    plan.set_defaults(func=cmd_plan)

    check = sub.add_parser("check", help="主动查一次（里程碑/收尾）")
    check.add_argument("--milestone", action="store_true")
    check.set_defaults(func=cmd_check)

    handoff = sub.add_parser("handoff", help="生成交接文档")
    handoff.add_argument("--goal", required=True)
    handoff.add_argument("--slug", default=None)
    handoff.add_argument("--reason", default=None)
    handoff.add_argument("--cwd", default=None)
    handoff.set_defaults(func=cmd_handoff)

    selftest_cmd = sub.add_parser("selftest", help="离线自检，不需要真额度")
    selftest_cmd.set_defaults(func=cmd_selftest)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
