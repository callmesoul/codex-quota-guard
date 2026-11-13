#!/usr/bin/env python3
"""handoff_board.py — project / session / handoff panel for Codex.

Scans two independent data sources and joins them:

  ~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl   → sessions, grouped by cwd
  <project>/.handoff/<utc>-<slug>/handoff.json   → handoffs, with HANDOFF.md

Output is one self-contained HTML panel that can be shown three ways:

  scan                 JSON on stdout
  render --out FILE    HTML fragment (for inline visualization)
  serve                local HTTP panel at http://127.0.0.1:<port>/
  marker               print the inline visualization content reference

No third-party dependencies: stdlib only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import html
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
TEMPLATE = os.path.join(PLUGIN_ROOT, "assets", "panel.fragment.html")
DATA_SLOT = '<script type="application/json" id="hb-data">null</script>'

MARKER_OPEN = "\ue200visualize\ue202"
MARKER_CLOSE = "\ue201"


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


def codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def load_thread_names(home: str) -> dict[str, str]:
    """session_index.jsonl: {"id", "thread_name", "updated_at"} per line."""
    names: dict[str, str] = {}
    path = os.path.join(home, "session_index.jsonl")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = row.get("id")
                name = (row.get("thread_name") or "").strip()
                if sid and name:
                    names[sid] = name
    except OSError:
        pass
    return names


def first_line_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return json.loads(fh.readline())
    except (OSError, json.JSONDecodeError):
        return {}


def iso_from_mtime(path: str) -> str:
    try:
        ts = os.path.getmtime(path)
    except OSError:
        return ""
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_sessions(home: str) -> list[dict]:
    names = load_thread_names(home)
    pattern = os.path.join(home, "sessions", "*", "*", "*", "rollout-*.jsonl")
    sessions = []
    for path in glob.glob(pattern):
        meta = first_line_json(path).get("payload") or {}
        sid = meta.get("session_id") or meta.get("id") or os.path.basename(path)
        cwd = (meta.get("cwd") or "").rstrip("/") or ""
        if not cwd:
            continue
        sessions.append(
            {
                "id": sid,
                "cwd": cwd,
                "title": names.get(sid) or "",
                "updated_at": meta.get("timestamp") or iso_from_mtime(path),
                "rollout": path,
                "model": meta.get("model_provider") or "",
                "originator": meta.get("originator") or "",
            }
        )
    sessions.sort(key=lambda s: s["updated_at"], reverse=True)
    return sessions


def find_handoff_dirs(project_path: str) -> list[str]:
    return sorted(glob.glob(os.path.join(project_path, ".handoff", "*", "handoff.json")))


def read_markdown(path: str, limit: int = 400_000) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def collect_handoffs(project_path: str) -> list[dict]:
    out = []
    for json_path in find_handoff_dirs(project_path):
        hdir = os.path.dirname(json_path)
        try:
            with open(json_path, encoding="utf-8", errors="replace") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            doc = {}
        src = doc.get("source") or {}
        quota = doc.get("quota") or {}
        repo = doc.get("repo") or {}
        md_path = os.path.join(hdir, "HANDOFF.md")
        out.append(
            {
                "id": os.path.basename(hdir),
                "dir": hdir,
                "created_at": doc.get("created_at") or "",
                "status": doc.get("status") or "",
                "reason": doc.get("reason") or "",
                "goal": doc.get("goal") or "",
                "session_id": src.get("session_id") or "",
                "agent": src.get("agent") or "",
                "verdict": quota.get("verdict") or "",
                "branch": repo.get("branch") or "",
                "head": (repo.get("head") or "")[:12],
                "dirty": repo.get("dirty"),
                "next_action": doc.get("next_action") or "",
                "todo": doc.get("todo") or [],
                "markdown": read_markdown(md_path),
                "has_markdown": os.path.exists(md_path),
            }
        )
    out.sort(key=lambda h: h["created_at"] or h["id"], reverse=True)
    return out


def candidate_roots(sessions: list[dict], extra: list[str]) -> list[str]:
    roots: list[str] = []
    seen = set()
    for path in extra + [s["cwd"] for s in sessions]:
        path = os.path.abspath(os.path.expanduser(path)).rstrip("/")
        if not path or path in seen:
            continue
        if not os.path.isdir(path):
            continue
        seen.add(path)
        roots.append(path)
    return roots


def build_board(roots: list[str] | None = None) -> dict:
    home = codex_home()
    all_sessions = collect_sessions(home)
    wanted = candidate_roots(all_sessions, roots or [])

    by_project: dict[str, list[dict]] = {r: [] for r in wanted}
    for s in all_sessions:
        if s["cwd"] in by_project:
            by_project[s["cwd"]].append(s)

    projects = []
    for path in wanted:
        sessions = by_project.get(path, [])
        handoffs = collect_handoffs(path)
        handoff_by_session: dict[str, list[str]] = {}
        for h in handoffs:
            handoff_by_session.setdefault(h["session_id"], []).append(h["id"])
        for s in sessions:
            s["handoff_ids"] = handoff_by_session.get(s["id"], [])
        recent = max(
            [s["updated_at"] for s in sessions] + [h["created_at"] for h in handoffs] + [""]
        )
        projects.append(
            {
                "path": path,
                "name": os.path.basename(path)
                or (os.path.basename(os.path.dirname(path)) or path),
                "sessions": sessions,
                "handoffs": handoffs,
                "session_count": len(sessions),
                "handoff_count": len(handoffs),
                "updated_at": recent,
                "is_git": os.path.isdir(os.path.join(path, ".git")),
            }
        )

    projects.sort(
        key=lambda p: (p["handoff_count"] > 0, p["updated_at"]), reverse=True
    )

    return {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "codex_home": home,
        "stats": {
            "projects": len([p for p in projects if p["session_count"] or p["handoff_count"]]),
            "sessions": sum(p["session_count"] for p in projects),
            "handoffs": sum(p["handoff_count"] for p in projects),
        },
        "projects": projects,
    }


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def load_template() -> str:
    with open(TEMPLATE, encoding="utf-8") as fh:
        return fh.read()


def render_fragment(board: dict) -> str:
    """Self-contained HTML fragment (no doctype/html/head/body).

    The template is the Vite build output: one inline <style>, one inline IIFE
    bundle, and a JSON script tag holding the data. Keeping the payload in a
    script tag (rather than inside the JS) means minification cannot rewrite it
    and the JSON stays readable for debugging.
    """
    payload = json.dumps(board, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")
    template = load_template()
    if DATA_SLOT not in template:
        raise RuntimeError("panel.fragment.html is missing the hb-data slot")
    return template.replace(
        DATA_SLOT,
        f'<script type="application/json" id="hb-data">{payload}</script>',
    )


STANDALONE_TOKENS = """
    /* The fragment inherits --background/--foreground/... from whoever renders
       it: the Codex visualization host supplies them inline, and this page
       supplies them for the served/standalone view. Keep the names identical
       so one stylesheet serves both. */
    :root {
      color-scheme: light dark;
      --background: #ffffff;
      --foreground: #14161a;
      --card: #f7f8fa;
      --card-foreground: #14161a;
      --popover: #ffffff;
      --popover-foreground: #14161a;
      --primary: #1a6dd6;
      --primary-foreground: #ffffff;
      --secondary: #eceef2;
      --secondary-foreground: #14161a;
      --muted: #f0f1f4;
      --muted-foreground: #5c626d;
      --accent: #e5f0fd;
      --accent-foreground: #1a6dd6;
      --destructive: #c2410c;
      --border: #dcdfe5;
      --input: #d3d7de;
      --ring: #1a6dd6;
      --radius: 0.625rem;
      --font-size-base: 14px;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --background: #14161a;
        --foreground: #e8eaee;
        --card: #1b1e24;
        --card-foreground: #e8eaee;
        --popover: #1b1e24;
        --popover-foreground: #e8eaee;
        --primary: #83c3ff;
        --primary-foreground: #0d0d0d;
        --secondary: #262a32;
        --secondary-foreground: #e8eaee;
        --muted: #22262d;
        --muted-foreground: #9aa1ad;
        --accent: #0d273f;
        --accent-foreground: #83c3ff;
        --destructive: #ff8549;
        --border: #2e333c;
        --input: #3a404a;
        --ring: #83c3ff;
      }
    }
    html, body { margin: 0; }
    body {
      background: var(--background);
      color: var(--foreground);
      font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      -webkit-font-smoothing: antialiased;
    }
    #hb-page {
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px 20px 64px;
    }
"""


def render_standalone(board: dict) -> str:
    fragment = render_fragment(board)
    title = "Handoff Board"
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n<style>{STANDALONE_TOKENS}</style>\n</head>\n<body>\n"
        '<main id="hb-page">\n' + fragment + "\n</main>\n</body>\n</html>\n"
    )


def default_out_path() -> str:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    home = codex_home()
    thread_dir = os.environ.get("CODEX_THREAD_ID") or ""
    if thread_dir:
        base = os.path.join(home, "visualizations", "handoff-board", thread_dir)
    else:
        base = os.path.join(home, "visualizations", "handoff-board")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        base = os.path.join("/tmp", "handoff-board")
        os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"handoff-board-{stamp}.html")


# --------------------------------------------------------------------------
# serving
# --------------------------------------------------------------------------


def serve(port: int, roots: list[str] | None = None) -> None:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                # rebuild per request: the panel follows disk, not a stale snapshot
                data = build_board(roots)
                self._send(200, render_standalone(data).encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/board":
                body = json.dumps(build_board(roots), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif path == "/healthz":
                self._send(200, b"ok", "text/plain; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        def log_message(self, fmt: str, *args) -> None:  # quiet
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"handoff board listening on {url}", flush=True)
    print("press ctrl-c to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped", flush=True)
    finally:
        server.server_close()


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="handoff_board.py", description=__doc__)
    ap.add_argument(
        "command",
        nargs="?",
        default="scan",
        choices=["scan", "render", "serve", "marker"],
    )
    ap.add_argument("--root", action="append", default=[], help="extra project path to scan")
    ap.add_argument("--out", help="output file for `render`")
    ap.add_argument("--standalone", action="store_true", help="render a full HTML document")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--compact", action="store_true", help="omit sessions without handoffs")
    args = ap.parse_args(argv)

    board = build_board(args.root)
    if args.compact:
        # keep only what produced a handoff: projects with none drop out entirely,
        # and a kept project shows only the sessions that own a handoff.
        projects = []
        for p in board["projects"]:
            if not p["handoff_count"]:
                continue
            p["sessions"] = [s for s in p["sessions"] if s.get("handoff_ids")]
            projects.append(p)
        board["projects"] = projects
        board["stats"] = {
            "projects": len(projects),
            "sessions": sum(p["session_count"] for p in projects),
            "handoffs": sum(p["handoff_count"] for p in projects),
        }

    if args.command == "scan":
        json.dump(board, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    if args.command in ("render", "marker"):
        out = args.out or default_out_path()
        body = render_standalone(board) if args.standalone else render_fragment(board)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(body)
        marker = MARKER_OPEN + json.dumps({"path": out, "title": "Handoff Board"}, ensure_ascii=False) + MARKER_CLOSE
        if args.command == "marker":
            print(marker)
        else:
            sys.stderr.write(f"wrote {out} ({len(body) / 1024:.1f} KiB)\n")
            print(out)
        return 0

    serve(args.port, args.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
