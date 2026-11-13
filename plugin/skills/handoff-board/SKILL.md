---
name: handoff-board
description: "Show every project, session and handoff document Codex has worked on as one browsable panel. Use when asked to list handoff documents, review what was left for the next session, or see which projects have pending handoffs."
---

# Handoff Board

Reads two stores and joins them:

- `~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl` — every session, grouped by its `cwd`.
- `<project>/.handoff/<utc>-<slug>/handoff.json` plus `HANDOFF.md` — every handoff written by `codex-quota-guard`.

## Locate the script

`handoff_board.py` sits in the `scripts/` directory next to the plugin root, so from this skill it is `../../scripts/handoff_board.py`. Resolve it once, then reuse that path:

```bash
BOARD="$(cd <plugin-root> && pwd)/scripts/handoff_board.py"
```

The installed copy is also under `~/.codex/plugins/cache/*/handoff-board/*/scripts/handoff_board.py`.

Everything is read relative to `CODEX_HOME` (falling back to `~/.codex`), so the board shows the
sessions of whichever home the process runs under. The desktop app and a WSL shell can point at
different homes; if the panel looks emptier than expected, check `echo "$CODEX_HOME"`.

## Show the panel

```bash
python3 "$BOARD" marker
```

That writes the HTML fragment to a durable per-thread directory and prints the inline visualization content reference. Paste that line verbatim, alone on its own line, where the panel should appear in the reply:

```text
visualize{"path":"<absolute path printed by the command>"}
```

In the real message the words `visualize` are wrapped in the private-use characters U+E200 / U+E202 / U+E201 exactly as printed by the command. Never reformat that line, never wrap it in backticks, and never describe it as a file or a link.

## Narrow it down

- Extra roots outside the session store, or one project only:
  `python3 "$BOARD" marker --root /path/to/project` (repeat `--root` for several)
- Only projects and sessions that produced a handoff:
  `python3 "$BOARD" marker --compact`
- Data instead of a panel, when the user wants a listing rather than a view:
  `python3 "$BOARD" scan`

Without `--root` the board covers every `cwd` found in the session store.

## Standalone web panel

When the user wants the panel outside the conversation, or the session store lives on another machine:

```bash
python3 "$BOARD" serve --port 8787
```

It prints a `http://127.0.0.1:<port>/` URL and serves the same view, rebuilding from disk on each request; `/api/board` returns the JSON. Run it as a background job rather than blocking on it.

For a file instead of a server:

```bash
python3 "$BOARD" render --standalone --out ~/handoff-board.html
```

## Reading the panel

- Left: projects, with session and handoff counts.
- Middle: sessions of the selected project. A row marked `handoff only` is a handoff whose rollout is missing locally — the document exists, the transcript does not.
- Right: the handoff document, with status, quota verdict, branch/HEAD and the rendered `HANDOFF.md`.

The **Ask Codex** button sends a follow-up asking to read that `HANDOFF.md` and take over the unfinished work, then reports the result under the document title (`Sent · Codex starts a new turn in this chat`, `Copied · paste it into the composer`, or the host's refusal reason). **Copy** puts the same prompt on the clipboard without touching the host; when neither path works the prompt is shown in a read-only box under the document.

## Handling the result

Answer with the panel and at most one or two sentences: which project has pending handoffs, or what the newest handoff asks the next session to do. Do not restate the document — the user is reading it in the panel.
