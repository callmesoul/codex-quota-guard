# dsh-handoff-board

DeepSeek Harness plugin that turns the Handoff Board into a first-class panel.

- Adds a **交接板** row to the sidebar panel list, between 自动化任务 and 更多.
- Clicking it opens the board as a **main panel** (same slot as the Plugins page).
- The board's ask button starts a real DSH session: `uiWorkspace.startSession()`
  opens (or reuses) a session in the workspace that owns the handoff project and
  injects the takeover prompt as its draft.

The prompt is **only prefilled, never auto-submitted** — the user presses Enter.
Auto-sending would start burning quota from a single misclick.

## Layout

| File | Role |
| --- | --- |
| `lib/index.js` | Host half: registers the `/handoff-board` HTTP prefix route and serves the board JSON (Node port of `plugin/scripts/handoff_board.py`). |
| `lib/client.js` | Browser half: `sidebar.panellist` icon + `main` keyed panel, mounts the Vue board, installs the `window.__HANDOFF_BOARD_DSH__` bridge. |
| `cordis.patch.yml` | Bundle patch that inserts the host row. |

## Payload extras

Each project carries two fields the Codex fragment does not need:

- `local_path` — the same directory as this host sees it (`\\wsl.localhost\Ubuntu\...`
  on Windows, `/home/...` on POSIX). Codex records Linux cwds; DSH stores the
  Windows/UNC form.
- `workspace_id` — the DSH workspace whose path matches `local_path`, or `null`.
  The client passes it to `uiWorkspace.startSession()` so the new session lands
  in the right project; `null` falls back to the current/recent workspace.

## Service access

The bridge resolves `uiWorkspace` through three paths, in order, and does it per
click rather than caching at boot:

1. plain property access on the context (`ctx.uiWorkspace`) — the form every
   official plugin uses, because `Context.extend()` is prototype-based;
2. `ctx.get('uiWorkspace')` — the reflect mixin;
3. `ctx.reflect.get('uiWorkspace', false)` — the raw store read, which skips the
   strict "providing fiber must be ACTIVE" check.

Only `undefined` from all three raises `DSH 会话服务不可用`, and that path logs
`[handoff-board] uiWorkspace unavailable; known services: ...` so the next failure
names what the registry actually held.

`startSession` is synchronous and reveals the conversation itself (`replaceMain`
ends with `layout.selectPanel(null)` when it is called with `"reveal"`). Do not
call `selectPanel()` after it: `selectPanel` and `beginNavigation` both abort the
pending navigation, and `openWorkspaceSession` drops the session when
`navigation.aborted` is set.

## Build

```bash
cd panel/handoff-board-panel
bash scripts/build-dsh-client.sh   # vue-tsc -b + vite build --config vite.dsh.config.ts + wrap
```

The client bundle is written to `dsh-plugin/lib/client.js` through
`scripts/wrap-dsh-client.mjs`, which wraps the CommonJS output in the
`window.__ModuleLoader__.load({ id, factory })` shell DSH expects.

## Install

```bash
# stage out of the WSL share first: pnpm cannot install from a UNC path
cp -r dsh-plugin "$LOCALAPPDATA/dsh-handoff-board-stage"
dsh plugin --profile desktop add "file:$LOCALAPPDATA/dsh-handoff-board-stage"
```

Restart the app afterwards: the `dsh.client` manifest is read when the entry boots.