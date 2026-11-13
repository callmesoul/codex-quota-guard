import type { Board } from './lib/types'

declare global {
  interface Window {
    __HANDOFF_BOARD__?: Board | null
    openai?: {
      sendFollowUpMessage?: (message: { prompt: string; title?: string }) => unknown
    }
  }
}

export {}
