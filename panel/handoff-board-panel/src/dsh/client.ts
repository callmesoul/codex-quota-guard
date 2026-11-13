/**
 * Browser half of the Handoff Board plugin.
 *
 * Two slot registrations, mirroring the official sidebar/panel pairing:
 *   - `sidebar.panellist` (list slot, `id`)    -> the icon row under 新会话
 *   - `main`              (keyed slot, `key`)  -> the board itself
 * The two ids must be identical: the sidebar calls `selectPanel(id)` and the
 * layout resolves the `main` entry whose `options.key` equals that id.
 *
 * The board UI is the existing Vue app from `panel/handoff-board-panel`; React
 * only owns the slot shells, so `react` stays external while Vue is bundled.
 */
import { createElement, useEffect, useRef } from 'react'
import { createApp } from 'vue'
import PanelApp from '@/App.vue'
import panelCss from '@/assets/index.css?inline'
import type { BoardBridge } from '@/lib/data'

const PACKAGE = 'dsh-handoff-board'
const PANEL_ID = 'handoff-board'
const PANEL_LABEL = '交接板'
const BOARD_URL = '/handoff-board/board'
const CSS_KEY = `${PACKAGE}/handoff-board.css`

/** The slice of the cordis client context this plugin touches. */
interface SlotRegisterOptions {
  name: string
  id?: string
  key?: string
  order?: number
  label?: string
}

interface SlotsService {
  inject(name: string, run: () => unknown): unknown
  register(options: SlotRegisterOptions, component: unknown): unknown
}

interface UiWorkspaceService {
  startSession(
    workspaceId: string | undefined,
    options?: { prompt?: string; whenDraftExists?: string },
  ): Promise<unknown>
}

interface ClientContext {
  inject(names: string[], run: (scoped: ClientContext) => void): unknown
  slots: SlotsService
  /** Injected services resolve as plain properties on the scoped context. */
  readonly uiWorkspace?: UiWorkspaceService
  /** `ctx.get` is the reflect mixin: available but non-strict about state. */
  get?(name: string): unknown
  /** Escape hatch: the raw service store, bypassing the inject requirement. */
  readonly reflect?: { get?(name: string, strict?: boolean): unknown }
}

/**
 * Resolve a service the way cordis actually exposes them.
 *
 * The documented path is plain property access on the context handed to an
 * `inject()` callback, but that only resolves for services the fiber declares.
 * `ctx.get()` is the reflect mixin and refuses implementations whose fiber is
 * mid-transition, and `ctx.reflect.get(name, false)` reads the store directly.
 * Trying all three keeps the bridge alive across those states.
 */
function resolveService<T>(ctx: ClientContext, name: string): T | undefined {
  try {
    const direct = (ctx as unknown as Record<string, unknown>)[name]
    if (direct) return direct as T
  } catch {
    /* not declared on this fiber: fall through */
  }
  try {
    const viaGet = ctx.get?.(name)
    if (viaGet) return viaGet as T
  } catch {
    /* inactive implementation: fall through */
  }
  try {
    const viaStore = ctx.reflect?.get?.(name, false)
    if (viaStore) return viaStore as T
  } catch {
    /* nothing registered: fall through */
  }
  return undefined
}

/** Compact diagnostics so the next failure names the services it could see. */
function describeServices(ctx: ClientContext): string {
  try {
    const props = (ctx as unknown as { reflect?: { props?: Record<string, unknown> } }).reflect?.props
    if (!props) return 'no service registry'
    const names = Object.keys(props)
    return names.length ? `known services: ${names.join(', ')}` : 'service registry is empty'
  } catch {
    return 'service registry unreadable'
  }
}

/** Copy the built stylesheet in once, tagged so a reload cannot double it. */
function injectStyles() {
  if (document.querySelector(`style[data-plugin-css="${CSS_KEY}"]`)) return
  const tag = document.createElement('style')
  tag.dataset.plugin = PACKAGE
  tag.dataset.pluginCss = CSS_KEY
  tag.textContent = panelCss
  document.head.appendChild(tag)
}

/** One broken segment must not take the whole shell boot down with it. */
function guarded(label: string, run: () => unknown): unknown {
  try {
    return run()
  } catch (error) {
    console.error(`[handoff-board] ${label} failed`, error)
    return undefined
  }
}

/** The sidebar renders this at 16px (wide) or 18px (collapsed). */
function HandoffBoardIcon(props: { size?: number }) {
  const size = props?.size ?? 16
  return createElement(
    'svg',
    {
      width: size,
      height: size,
      viewBox: '0 0 24 24',
      fill: 'none',
      stroke: 'currentColor',
      strokeWidth: 1.8,
      strokeLinecap: 'round',
      strokeLinejoin: 'round',
      'aria-hidden': 'true',
      focusable: 'false',
    },
    createElement('rect', { key: 'page', x: 2.75, y: 4.75, width: 12.5, height: 14.5, rx: 2.5 }),
    createElement('path', { key: 'lines', d: 'M6.25 9.4h6M6.25 12.4h4' }),
    createElement('path', { key: 'shaft', d: 'M15.5 12h5.6' }),
    createElement('path', { key: 'head', d: 'M18.7 8.9 21.8 12 18.7 15.1' }),
  )
}

/**
 * Mount the Vue board into a React-owned div. `#hb-root` is the id the panel's
 * stylesheet scopes its preflight to, so the host page is left untouched.
 */
function HandoffBoardPage() {
  const host = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    const el = host.current
    if (!el) return undefined
    const app = createApp(PanelApp)
    app.mount(el)
    return () => {
      try {
        app.unmount()
      } catch (error) {
        console.error('[handoff-board] panel unmount failed', error)
      }
    }
  }, [])
  return createElement('div', { ref: host, id: 'hb-root', className: 'hb-dsh-panel' })
}

/**
 * Bridge the panel's ask button onto DSH. The board payload carries the
 * `workspace_id` the host half matched, so the session opens in the project the
 * handoff belongs to; without a match DSH falls back to the current/recent
 * workspace, exactly like the official schedule panel.
 */
function installBridge(ctx: ClientContext) {
  const bridge: BoardBridge = window.__HANDOFF_BOARD_DSH__ ?? { boardUrl: BOARD_URL }
  bridge.boardUrl = BOARD_URL
  bridge.startSession = async (request) => {
    // Resolve per click: a service can be absent while its own plugin is still
    // starting, and the bridge outlives that window.
    const uiWorkspace = resolveService<UiWorkspaceService>(ctx, 'uiWorkspace')
    if (!uiWorkspace || typeof uiWorkspace.startSession !== 'function') {
      console.error('[handoff-board] uiWorkspace unavailable;', describeServices(ctx))
      throw new Error('DSH 会话服务不可用')
    }
    const prompt = String(request?.prompt ?? '')
    if (!prompt) throw new Error('交接提示词为空')
    const workspaceId = request?.project?.workspace_id ?? undefined
    // `startSession` is synchronous and already reveals the conversation: it
    // routes through `openWorkspace(..., {panel: "reveal"})`, whose `replaceMain`
    // ends with `layout.selectPanel(null)`. Calling `selectPanel(null)` here
    // would abort the navigation that call is still awaiting, and the session
    // would be created but never shown.
    uiWorkspace.startSession(workspaceId, { prompt, whenDraftExists: 'append' })
  }
  window.__HANDOFF_BOARD_DSH__ = bridge
}

export const inject = ['slots']

export function apply(ctx: ClientContext) {
  window.__HANDOFF_BOARD_DSH__ = { boardUrl: BOARD_URL, startSession: null }

  guarded('stylesheet injection', injectStyles)
  // Install against the plugin context first: the click then always reaches a
  // bridge and reports a real reason, instead of silently finding `null`.
  guarded('session bridge', () => installBridge(ctx))
  guarded('session bridge (scoped)', () => {
    ctx.inject(['uiWorkspace'], (scoped) => installBridge(scoped))
  })

  guarded('sidebar panel entry', () => {
    ctx.slots.inject('sidebar.panellist', () =>
      ctx.slots.register(
        { name: 'sidebar.panellist', id: PANEL_ID, order: 20, label: PANEL_LABEL },
        HandoffBoardIcon,
      ),
    )
  })

  guarded('main panel', () => {
    ctx.slots.inject('main', () =>
      ctx.slots.register({ name: 'main', key: PANEL_ID }, HandoffBoardPage),
    )
  })
}

export { name } from './name.js'