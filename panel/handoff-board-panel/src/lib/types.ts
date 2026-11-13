export interface Handoff {
  id: string
  dir: string
  created_at: string | null
  status: string | null
  reason: string | null
  goal: string | null
  session_id: string | null
  agent: string | null
  verdict: string | null
  branch: string | null
  head: string | null
  dirty: boolean | null
  next_action: string | null
  todo: unknown[]
  markdown: string | null
  has_markdown: boolean
}

export interface Session {
  id: string
  cwd: string
  title: string | null
  updated_at: string | null
  rollout?: string | null
  model?: string | null
  originator?: string | null
  handoff_ids: string[]
}

export interface Project {
  path: string
  /** The same directory as the host filesystem sees it (DSH payload only). */
  local_path?: string
  /** DSH workspace whose path matches local_path; null when unmatched. */
  workspace_id?: string | null
  name: string
  sessions: Session[]
  handoffs: Handoff[]
  session_count: number
  handoff_count: number
  updated_at: string | null
  is_git: boolean
}

export interface BoardStats {
  projects: number
  sessions: number
  handoffs: number
}

export interface Board {
  generated_at: string
  codex_home: string
  stats: BoardStats
  projects: Project[]
}