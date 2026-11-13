/**
 * Host half of the Handoff Board plugin.
 *
 * It owns one HTTP prefix route (`/handoff-board`) that serves the board
 * payload the browser half renders:
 *
 *   GET /handoff-board/board    -> Board JSON (projects, sessions, handoffs)
 *   GET /handoff-board/health   -> { ok: true }
 *
 * The scan is a Node port of `plugin/scripts/handoff_board.py` (the Codex
 * plugin's scanner) plus one extra field per project: `workspace_id`, the DSH
 * workspace whose path matches the project directory. The browser half passes
 * it to `uiWorkspace.startSession()` so the new session opens in the right
 * workspace instead of wherever the user happens to be.
 *
 * Path translation matters here: Codex records Linux cwds (`/home/...`,
 * `/mnt/c/...`) while DSH stores Windows or `\\wsl.localhost\...` paths.
 */
import { promises as fs } from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

const ROUTE = '/handoff-board'
const CACHE_TTL_MS = 3000
const MARKDOWN_LIMIT = 400_000
const DEFAULT_WSL_PREFIX = '\\\\wsl.localhost\\Ubuntu'

const name = 'handoff-board'

/** Codex home: `$CODEX_HOME` or `~/.codex`. */
function codexHome() {
  const override = (process.env.CODEX_HOME ?? '').trim()
  return override || path.join(os.homedir(), '.codex')
}

/** DSH home: `$DSH_HOME` or `~/.dsh`. */
function dshHome() {
  const override = (process.env.DSH_HOME ?? '').trim()
  return override || path.join(os.homedir(), '.dsh')
}

async function readJson(file) {
  try {
    return JSON.parse(await fs.readFile(file, 'utf8'))
  } catch {
    return null
  }
}

async function statOrNull(file) {
  try {
    return await fs.stat(file)
  } catch {
    return null
  }
}

function isoFromMtime(ms) {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')
}

/** First line of a rollout file, parsed. Rollouts open with a `session_meta`. */
async function firstLineJson(file) {
  let handle
  try {
    handle = await fs.open(file, 'r')
    const buffer = Buffer.allocUnsafe(64 * 1024)
    const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0)
    const text = buffer.subarray(0, bytesRead).toString('utf8')
    const end = text.indexOf('\n')
    return JSON.parse(end === -1 ? text : text.slice(0, end))
  } catch {
    return null
  } finally {
    await handle?.close().catch(() => {})
  }
}

/** `{ id: thread_name }` from `session_index.jsonl`. */
async function loadThreadNames(home) {
  const names = new Map()
  let text
  try {
    text = await fs.readFile(path.join(home, 'session_index.jsonl'), 'utf8')
  } catch {
    return names
  }
  for (const line of text.split('\n')) {
    if (!line.trim()) continue
    try {
      const entry = JSON.parse(line)
      if (entry?.id) names.set(entry.id, entry.thread_name ?? '')
    } catch {
      /* one broken line must not lose the rest of the index */
    }
  }
  return names
}

async function listRollouts(home) {
  const files = []
  const sessions = path.join(home, 'sessions')
  let years
  try {
    years = await fs.readdir(sessions, { withFileTypes: true })
  } catch {
    return files
  }
  for (const year of years) {
    if (!year.isDirectory()) continue
    let months
    try {
      months = await fs.readdir(path.join(sessions, year.name), { withFileTypes: true })
    } catch {
      continue
    }
    for (const month of months) {
      if (!month.isDirectory()) continue
      let days
      try {
        days = await fs.readdir(path.join(sessions, year.name, month.name), { withFileTypes: true })
      } catch {
        continue
      }
      for (const day of days) {
        if (!day.isDirectory()) continue
        const dir = path.join(sessions, year.name, month.name, day.name)
        let entries
        try {
          entries = await fs.readdir(dir)
        } catch {
          continue
        }
        for (const entry of entries) {
          if (entry.startsWith('rollout-') && entry.endsWith('.jsonl')) files.push(path.join(dir, entry))
        }
      }
    }
  }
  return files
}

async function collectSessions(home) {
  const names = await loadThreadNames(home)
  const files = await listRollouts(home)
  const sessions = []
  for (const file of files) {
    const meta = (await firstLineJson(file))?.payload
    if (!meta || typeof meta !== 'object') continue
    const cwd = String(meta.cwd ?? '').replace(/\/+$/, '')
    if (!cwd) continue
    const id = String(meta.session_id ?? meta.id ?? path.basename(file, '.jsonl'))
    const stat = await statOrNull(file)
    sessions.push({
      id,
      cwd,
      title: names.get(id) ?? '',
      updated_at: String(meta.timestamp ?? (stat ? isoFromMtime(stat.mtimeMs) : '')),
      rollout: file,
      model: String(meta.model_provider ?? meta.model ?? ''),
      originator: String(meta.originator ?? ''),
      handoff_ids: [],
    })
  }
  sessions.sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))
  return sessions
}

async function readMarkdown(file) {
  try {
    const handle = await fs.open(file, 'r')
    try {
      const buffer = Buffer.allocUnsafe(MARKDOWN_LIMIT)
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0)
      return buffer.subarray(0, bytesRead).toString('utf8')
    } finally {
      await handle.close().catch(() => {})
    }
  } catch {
    return ''
  }
}

async function collectHandoffs(projectPath) {
  const root = path.join(projectPath, '.handoff')
  let entries
  try {
    entries = await fs.readdir(root, { withFileTypes: true })
  } catch {
    return []
  }
  const handoffs = []
  for (const entry of entries) {
    if (!entry.isDirectory()) continue
    const dir = path.join(root, entry.name)
    const spec = await readJson(path.join(dir, 'handoff.json'))
    if (!spec) continue
    const source = spec.source ?? {}
    const quota = spec.quota ?? {}
    const repo = spec.repo ?? {}
    const markdown = await readMarkdown(path.join(dir, 'HANDOFF.md'))
    handoffs.push({
      id: entry.name,
      dir,
      created_at: String(spec.created_at ?? ''),
      status: String(spec.status ?? ''),
      reason: String(spec.reason ?? ''),
      goal: String(spec.goal ?? ''),
      session_id: String(source.session_id ?? ''),
      agent: String(source.agent ?? ''),
      verdict: String(quota.verdict ?? ''),
      branch: String(repo.branch ?? ''),
      head: String(repo.head ?? '').slice(0, 12),
      dirty: repo.dirty ?? null,
      next_action: String(spec.next_action ?? ''),
      todo: Array.isArray(spec.todo) ? spec.todo : [],
      markdown,
      has_markdown: markdown.length > 0,
    })
  }
  handoffs.sort((a, b) => (b.created_at || b.id).localeCompare(a.created_at || a.id))
  return handoffs
}

/** DSH workspaces, keyed by their path, from `<dsh>/storages/workspace.json`. */
async function loadWorkspaces() {
  const store = await readJson(path.join(dshHome(), 'storages', 'workspace.json'))
  const table = store?.tables?.workspaces
  const workspaces = []
  if (table && typeof table === 'object') {
    for (const [workspaceId, value] of Object.entries(table)) {
      if (!value || typeof value !== 'object') continue
      workspaces.push({
        workspace_id: workspaceId,
        path: String(value.path ?? ''),
        title: String(value.title ?? ''),
      })
    }
  }
  return workspaces
}

/**
 * Map a Codex cwd onto the path the same directory has for this host.
 *
 * Codex records the cwd it ran with: a Linux path when it ran under WSL. DSH on
 * Windows stores the same directory as `\\wsl.localhost\Ubuntu\...` or `C:\...`.
 * Each DSH workspace gives us one (windows, linux) pair to translate with, so we
 * pair a UNC root with the Linux prefix that mounts it, longest prefix first.
 */
function translator(workspaces) {
  const mounts = []
  for (const workspace of workspaces) {
    const match = /^(\\\\[^\\]+\\[^\\]+)(\\.*)?$/.exec(workspace.path)
    if (!match) continue
    const windows = match[1] + (match[2] ?? '')
    const linux = (match[2] ?? '/').replace(/\\/g, '/') || '/'
    mounts.push({ windows, linux })
  }
  mounts.push({ windows: DEFAULT_WSL_PREFIX, linux: '/' })
  mounts.sort((a, b) => b.linux.length - a.linux.length)

  /** Windows/UNC form of a Linux cwd (used when this host is Windows). */
  const toWindows = (cwd) => {
    if (!cwd) return ''
    if (/^[A-Za-z]:[\\/]/.test(cwd) || cwd.startsWith('\\\\')) return cwd
    if (cwd.startsWith('/mnt/')) {
      const [, , drive, ...segments] = cwd.split('/')
      if (drive) return `${drive.toUpperCase()}:\\${segments.join('\\')}`
    }
    for (const mount of mounts) {
      if (mount.linux === '/' || cwd === mount.linux || cwd.startsWith(`${mount.linux}/`)) {
        const tail = mount.linux === '/' ? cwd : cwd.slice(mount.linux.length)
        return mount.windows + tail.replace(/\//g, '\\')
      }
    }
    return cwd
  }

  /** Linux form of a Windows/UNC path (used when this host is POSIX). */
  const toLinux = (value) => {
    if (!value) return ''
    if (value.startsWith('/')) return value
    const drive = /^([A-Za-z]):[\\/](.*)$/.exec(value)
    if (drive) return `/mnt/${drive[1].toLowerCase()}/${drive[2].replace(/\\/g, '/')}`
    for (const mount of mounts) {
      if (mount.linux === '/' || value === mount.windows || value.startsWith(`${mount.windows}\\`)) {
        const tail = value.slice(mount.windows.length).replace(/\\/g, '/')
        return (mount.linux === '/' ? '' : mount.linux) + tail
      }
    }
    return value
  }

  return { toWindows, toLinux }
}
function samePath(a, b) {
  return a.replace(/[\\/]+$/, '').toLowerCase() === b.replace(/[\\/]+$/, '').toLowerCase()
}

/**
 * The path this process can actually read for a Codex cwd: Codex records the cwd
 * it ran with, which is a Linux path when it ran under WSL, while DSH may store
 * the same directory as a Windows drive or UNC path.
 */
function nativePath(cwd, toLocal) {
  if (!cwd) return ''
  const direct = process.platform === 'win32' ? toLocal.toWindows(cwd) : toLocal.toLinux(cwd)
  return direct
}

async function buildBoard() {
  const home = codexHome()
  const sessions = await collectSessions(home)
  const workspaces = await loadWorkspaces()
  const toLocal = translator(workspaces)

  const projects = new Map()
  for (const session of sessions) {
    let project = projects.get(session.cwd)
    if (!project) {
      const local = nativePath(session.cwd, toLocal)
      const workspace = workspaces.find((entry) => samePath(entry.path, local))
      project = {
        path: session.cwd,
        local_path: local,
        name: path.basename(local) || path.basename(session.cwd) || session.cwd,
        workspace_id: workspace ? workspace.workspace_id : null,
        sessions: [],
        handoffs: [],
        session_count: 0,
        handoff_count: 0,
        updated_at: '',
        is_git: false,
      }
      projects.set(session.cwd, project)
    }
    project.sessions.push(session)
  }

  for (const project of projects.values()) {
    const stat = await statOrNull(project.local_path)
    if (stat?.isDirectory()) {
      const git = await statOrNull(path.join(project.local_path, '.git'))
      project.is_git = Boolean(git)
      project.handoffs = await collectHandoffs(project.local_path)
    }
    project.handoff_count = project.handoffs.length
    project.session_count = project.sessions.length
    for (const handoff of project.handoffs) {
      const owner = project.sessions.find((session) => session.id === handoff.session_id)
      if (owner && !owner.handoff_ids.includes(handoff.id)) owner.handoff_ids.push(handoff.id)
    }
    project.updated_at = project.sessions.reduce(
      (latest, session) => (String(session.updated_at) > latest ? String(session.updated_at) : latest),
      project.handoffs.reduce((latest, handoff) => (handoff.created_at > latest ? handoff.created_at : latest), ''),
    )
  }

  const list = [...projects.values()].sort((a, b) => {
    if ((a.handoff_count > 0) !== (b.handoff_count > 0)) return a.handoff_count > 0 ? -1 : 1
    return String(b.updated_at).localeCompare(String(a.updated_at))
  })

  return {
    generated_at: isoFromMtime(Date.now()),
    codex_home: home,
    stats: {
      projects: list.length,
      sessions: sessions.length,
      handoffs: list.reduce((total, project) => total + project.handoff_count, 0),
    },
    projects: list,
  }
}
/** Serve one request: JSON payloads only, no caching, failures as JSON errors. */
async function handle(req, res) {
  const url = new URL(req.url ?? '/', 'http://handoff-board.invalid')
  const route = url.pathname.slice(ROUTE.length).replace(/\/+$/, '') || '/'
  const send = (status, body) => {
    const payload = JSON.stringify(body)
    res.writeHead(status, {
      'content-type': 'application/json; charset=utf-8',
      'content-length': Buffer.byteLength(payload),
      'cache-control': 'no-store',
    })
    res.end(payload)
  }
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    res.writeHead(405, { allow: 'GET, HEAD', 'content-type': 'text/plain; charset=utf-8' })
    res.end('method not allowed')
    return
  }
  try {
    if (route === '/health') {
      send(200, { ok: true })
      return
    }
    if (route === '/' || route === '/board') {
      send(200, await buildBoard())
      return
    }
    send(404, { error: 'not found', route })
  } catch (error) {
    send(500, { error: error instanceof Error ? error.message : String(error) })
  }
}

/**
 * Cordis entry: register the route once a web server exists, and drop it again
 * when the plugin is disposed.
 */
export function apply(ctx) {
  let cache
  let pending

  const cachedBoard = async () => {
    const now = Date.now()
    if (cache && now - cache.at < CACHE_TTL_MS) return cache.value
    if (!pending) {
      pending = buildBoard().then(
        (value) => {
          cache = { at: Date.now(), value }
          return value
        },
        (error) => {
          throw error
        },
      ).finally(() => {
        pending = undefined
      })
    }
    return pending
  }

  ctx.inject(['webServer'], (webCtx) => {
    webCtx.effect(
      () =>
        webCtx.webServer.register({
          kind: 'prefix',
          path: ROUTE,
          handler: (req, res) => {
            const url = new URL(req.url ?? '/', 'http://handoff-board.invalid')
            const route = url.pathname.slice(ROUTE.length).replace(/\/+$/, '') || '/'
            if (route !== '/' && route !== '/board') return handle(req, res)
            if (req.method !== 'GET' && req.method !== 'HEAD') return handle(req, res)
            cachedBoard().then(
              (board) => {
                const payload = JSON.stringify(board)
                res.writeHead(200, {
                  'content-type': 'application/json; charset=utf-8',
                  'content-length': Buffer.byteLength(payload),
                  'cache-control': 'no-store',
                })
                res.end(payload)
              },
              (error) => {
                const payload = JSON.stringify({
                  error: error instanceof Error ? error.message : String(error),
                })
                res.writeHead(500, {
                  'content-type': 'application/json; charset=utf-8',
                  'content-length': Buffer.byteLength(payload),
                })
                res.end(payload)
              },
            )
          },
        }),
      'handoff-board.route',
    )
  })
}

export { name }
export default { name, apply }
