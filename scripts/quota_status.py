#!/usr/bin/env python3
"""Read Codex ChatGPT quota windows through the local Codex app-server."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import shutil
import subprocess
import sys
import time
from datetime import datetime
from typing import Any


CLIENT_INFO = {
    "name": "codex_quota_guard_skill",
    "title": "Codex Quota Guard Skill",
    "version": "1.0.0",
}


class AppServerError(RuntimeError):
    pass


def send(proc: subprocess.Popen[str], message: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def read_response(
    proc: subprocess.Popen[str],
    selector: selectors.BaseSelector,
    request_id: int,
    deadline: float,
    stderr_lines: list[str],
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            detail = "\n".join(stderr_lines[-8:]).strip()
            suffix = f": {detail}" if detail else ""
            raise AppServerError(f"codex app-server exited early{suffix}")

        remaining = max(0.0, deadline - time.monotonic())
        events = selector.select(min(remaining, 0.5))
        for key, _ in events:
            line = key.fileobj.readline()
            if not line:
                try:
                    selector.unregister(key.fileobj)
                except KeyError:
                    pass
                continue
            if key.data == "stderr":
                stderr_lines.append(line.rstrip())
                continue

            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                if isinstance(error, dict):
                    detail = error.get("message") or json.dumps(error, ensure_ascii=False)
                else:
                    detail = str(error)
                raise AppServerError(detail)
            result = message.get("result")
            if not isinstance(result, dict):
                raise AppServerError(f"unexpected response for request {request_id}")
            return result

    detail = "\n".join(stderr_lines[-8:]).strip()
    suffix = f"; app-server: {detail}" if detail else ""
    raise AppServerError(f"timed out waiting for request {request_id}{suffix}")


def fetch_rate_limits(timeout_seconds: float) -> dict[str, Any]:
    codex = shutil.which("codex")
    if not codex:
        raise AppServerError("codex executable was not found on PATH")

    proc = subprocess.Popen(
        [codex, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=os.environ.copy(),
    )
    assert proc.stdout is not None
    assert proc.stderr is not None

    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
    selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
    stderr_lines: list[str] = []
    deadline = time.monotonic() + timeout_seconds

    try:
        send(proc, {"method": "initialize", "id": 0, "params": {"clientInfo": CLIENT_INFO}})
        read_response(proc, selector, 0, deadline, stderr_lines)
        send(proc, {"method": "initialized", "params": {}})
        send(proc, {"method": "account/rateLimits/read", "id": 1})
        return read_response(proc, selector, 1, deadline, stderr_lines)
    finally:
        selector.close()
        if proc.stdin is not None:
            proc.stdin.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)


def normalized_buckets(result: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict) and by_id:
        buckets = [value for value in by_id.values() if isinstance(value, dict)]
    else:
        single = result.get("rateLimits")
        buckets = [single] if isinstance(single, dict) else []

    normalized: list[dict[str, Any]] = []
    for bucket in buckets:
        windows: list[dict[str, Any]] = []
        for kind in ("primary", "secondary"):
            window = bucket.get(kind)
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent")
            if isinstance(used, (int, float)):
                remaining = max(0.0, min(100.0, 100.0 - float(used)))
            else:
                remaining = None
            windows.append(
                {
                    "kind": kind,
                    "windowDurationMins": window.get("windowDurationMins"),
                    "usedPercent": used,
                    "remainingPercent": remaining,
                    "resetsAt": window.get("resetsAt"),
                }
            )
        normalized.append(
            {
                "limitId": bucket.get("limitId"),
                "limitName": bucket.get("limitName"),
                "planType": bucket.get("planType"),
                "rateLimitReachedType": bucket.get("rateLimitReachedType"),
                "windows": windows,
            }
        )
    return normalized


def format_number(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def format_reset(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")


def print_human(result: dict[str, Any]) -> None:
    buckets = normalized_buckets(result)
    if not buckets:
        raise AppServerError("the account returned no Codex rate-limit buckets")

    account = result.get("accountId")
    if account:
        print(f"Account: {account}")
    for bucket in buckets:
        name = bucket.get("limitName") or bucket.get("limitId") or "unknown bucket"
        print(f"Bucket: {name}")
        for window in bucket["windows"]:
            minutes = window.get("windowDurationMins")
            label = "5-hour" if minutes == 300 else f"{minutes}-minute"
            print(
                f"  {label} ({window['kind']}): "
                f"used {format_number(window.get('usedPercent'))}%, "
                f"remaining {format_number(window.get('remainingPercent'))}%, "
                f"resets {format_reset(window.get('resetsAt'))}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print normalized JSON")
    parser.add_argument("--raw-json", action="store_true", help="print the raw app-server result")
    parser.add_argument("--timeout", type=float, default=20.0, help="overall timeout in seconds")
    args = parser.parse_args()

    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    try:
        result = fetch_rate_limits(args.timeout)
        if args.raw_json:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        elif args.json:
            output = {
                "buckets": normalized_buckets(result),
                "rateLimitResetCredits": result.get("rateLimitResetCredits"),
                # 归属证据：切换账号后靠它判断这组额度属于谁
                "accountId": result.get("accountId"),
            }
            print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print_human(result)
        return 0
    except (AppServerError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
