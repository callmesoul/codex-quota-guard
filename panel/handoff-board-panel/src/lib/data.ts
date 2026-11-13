import type { Board, Project } from './types'

/** The bridge the DSH client half installs on `window` before mounting. */
export interface BoardBridge {
  boardUrl?: string
  startSession?: ((request: { prompt: string; project: Project | null }) => Promise<void>) | null
}

declare global {
  interface Window {
    __HANDOFF_BOARD__?: Board | null
    __HANDOFF_BOARD_DSH__?: BoardBridge
  }
}

/** The DSH plugin bridge, when the board runs inside the harness. */
export function dshBridge(): BoardBridge | null {
  const bridge = window.__HANDOFF_BOARD_DSH__
  return bridge && typeof bridge.startSession === 'function' ? bridge : null
}

/**
 * The board payload is emitted by handoff_board.py as a JSON script tag, which
 * keeps it out of the bundled JavaScript and immune to minifier rewriting.
 */
export function readBoardData(): Board | null {
  const node = document.getElementById('hb-data')
  if (node && node.textContent) {
    try {
      return JSON.parse(node.textContent) as Board
    } catch {
      return null
    }
  }
  return window.__HANDOFF_BOARD__ ?? null
}

/**
 * Load the board: inside DSH the host plugin serves it from /handoff-board,
 * otherwise fall back to the inline payload the Codex fragment ships.
 */
export async function fetchBoard(): Promise<Board | null> {
  const url = window.__HANDOFF_BOARD_DSH__?.boardUrl
  if (!url) return readBoardData()
  try {
    const response = await fetch(url, { headers: { accept: 'application/json' } })
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
    return (await response.json()) as Board
  } catch (error) {
    console.error('[handoff-board] board fetch failed', error)
    return readBoardData()
  }
}