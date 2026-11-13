import { computed, ref, watch } from 'vue'
import { dshBridge } from './data'
import type { Board, Handoff, Project, Session } from './types'

const board = ref<Board | null>(null)

export type AskState = 'idle' | 'sending' | 'sent' | 'copied' | 'failed'

export function ago(iso: string | null | undefined): string {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return String(iso).slice(0, 10)
  const s = Math.max(0, (Date.now() - t) / 1000)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`
  return new Date(t).toISOString().slice(0, 10)
}

export function sessionLabel(session: Session): string {
  return session.title || (session.id || '').slice(0, 8)
}

export function handoffLabel(handoff: Handoff): string {
  return handoff.id.replace(/^[0-9TZ]+-/, '')
}

export function useBoard() {
  const query = ref('')
  const onlyHandoffs = ref(false)
  const selectedProject = ref<string | null>(null)
  const selectedSession = ref<string | null>(null)
  const selectedHandoff = ref<string | null>(null)

  const askState = ref<AskState>('idle')
  const askNotice = ref('')
  const lastAskPrompt = ref('')

  const projects = computed<Project[]>(() => board.value?.projects ?? [])
  const stats = computed(() => board.value?.stats ?? { projects: 0, sessions: 0, handoffs: 0 })
  const normalizedQuery = computed(() => query.value.trim().toLowerCase())

  function match(text: unknown): boolean {
    if (!normalizedQuery.value) return true
    return String(text ?? '').toLowerCase().includes(normalizedQuery.value)
  }

  function projectMatches(project: Project): boolean {
    if (match(project.name) || match(project.path)) return true
    if ((project.sessions ?? []).some((s) => match(sessionLabel(s)) || match(s.id))) return true
    return (project.handoffs ?? []).some(
      (h) => match(h.goal) || match(h.id) || match(h.reason) || match(h.markdown),
    )
  }

  function findProject(path: string | null): Project | null {
    if (!path) return null
    return projects.value.find((p) => p.path === path) ?? null
  }

  function findHandoff(projectPath: string | null, id: string | null): Handoff | null {
    if (!id) return null
    return findProject(projectPath)?.handoffs?.find((h) => h.id === id) ?? null
  }

  const currentProject = computed(() => findProject(selectedProject.value))
  const currentHandoff = computed(() => findHandoff(selectedProject.value, selectedHandoff.value))

  /** The selected project always stays visible: the other columns keep showing it. */
  const visibleProjects = computed<Project[]>(() => {
    const shown = projects.value.filter((p) => {
      if (onlyHandoffs.value && !(p.handoff_count > 0) && p.path !== selectedProject.value) return false
      if (!normalizedQuery.value) return true
      return projectMatches(p)
    })
    const selected = findProject(selectedProject.value)
    if (selected && !shown.includes(selected)) shown.unshift(selected)
    return shown
  })

  const pinnedProject = computed(
    () => Boolean(normalizedQuery.value) && Boolean(currentProject.value) &&
      !projectMatches(currentProject.value as Project),
  )

  /** Handoffs whose rollout is missing locally (other machine, pruned store). */
  const orphanHandoffs = computed<Handoff[]>(() => {
    const project = currentProject.value
    if (!project) return []
    const known = new Set((project.sessions ?? []).map((s) => s.id))
    return (project.handoffs ?? []).filter((h) => h.session_id && !known.has(h.session_id))
  })

  const visibleSessions = computed<Session[]>(() => {
    const project = currentProject.value
    if (!project) return []
    let sessions = (project.sessions ?? []).slice()
    if (onlyHandoffs.value) sessions = sessions.filter((s) => (s.handoff_ids ?? []).length)
    if (normalizedQuery.value) {
      sessions = sessions.filter((s) => match(sessionLabel(s)) || match(s.id))
    }
    return sessions.slice(0, 200)
  })

  const handoffChoices = computed<Handoff[]>(() => {
    const project = currentProject.value
    if (!project) return []
    const handoffs = project.handoffs ?? []
    if (!selectedSession.value) return handoffs
    return handoffs.filter((h) => h.session_id === selectedSession.value)
  })

  function selectProject(path: string) {
    selectedProject.value = path
    selectedSession.value = null
    const project = findProject(path)
    const first = project?.handoffs?.[0] ?? null
    selectedHandoff.value = first ? first.id : null
    if (first) selectedSession.value = first.session_id ?? null
  }

  function selectSession(id: string) {
    selectedSession.value = id
    const project = currentProject.value
    if (project) {
      const session = (project.sessions ?? []).find((s) => s.id === id)
      const ids = session?.handoff_ids ?? []
      selectedHandoff.value = ids.length ? ids[0] : null
    }
  }

  function selectHandoff(id: string) {
    selectedHandoff.value = id
  }

  /** The prompt the Ask button would send for the handoff on screen. */
  const currentAskPrompt = computed(() => {
    const handoff = currentHandoff.value
    return handoff ? askPrompt(handoff) : ''
  })

  function askPrompt(handoff: Handoff): string {
    return (
      `继续这个交接：${handoff.id} (project ${selectedProject.value})。请先读 ` +
      `${handoff.dir}/HANDOFF.md，然后接管未完成的工作。`
    )
  }

  /**
   * Copy without awaiting anything first: `execCommand` keeps the click's
   * transient activation, and a sandboxed iframe may refuse `navigator.clipboard`.
   */
  function copyText(text: string): boolean {
    try {
      const area = document.createElement('textarea')
      area.value = text
      area.setAttribute('readonly', '')
      area.style.position = 'fixed'
      area.style.top = '0'
      area.style.left = '-9999px'
      area.style.opacity = '0'
      document.body.appendChild(area)
      area.select()
      area.setSelectionRange(0, text.length)
      const ok = document.execCommand('copy')
      document.body.removeChild(area)
      return ok
    } catch {
      return false
    }
  }

  let askTimer = 0
  let askLockUntil = 0

  function setAsk(state: AskState, notice: string) {
    askState.value = state
    askNotice.value = notice
    if (askTimer) window.clearTimeout(askTimer)
    if (state === 'sending') return
    askTimer = window.setTimeout(() => {
      askState.value = 'idle'
      askNotice.value = ''
    }, state === 'failed' ? 10_000 : 6000)
  }

  function failureNotice(error: unknown): string {
    const text = error instanceof Error ? error.message : String(error ?? '')
    if (/already being submitted/i.test(text)) {
      return 'Codex is still starting the last turn — copy the prompt instead.'
    }
    if (/disabled for this view/i.test(text)) {
      return 'This view blocks follow-ups — copy the prompt instead.'
    }
    return text ? `Request rejected: ${text}` : 'Request rejected.'
  }

  /** A DSH failure is the harness's, so it must not blame Codex. */
  function dshFailureNotice(error: unknown): string {
    const text = error instanceof Error ? error.message : String(error ?? '')
    return text ? `DSH 会话没有启动：${text}` : 'DSH 会话没有启动。'
  }

  function copyPrompt(handoff: Handoff): AskState {
    const prompt = askPrompt(handoff)
    lastAskPrompt.value = prompt
    if (copyText(prompt)) {
      setAsk('copied', 'Copied · paste it into the composer')
      return 'copied'
    }
    const clipboard = navigator.clipboard
    if (clipboard?.writeText) {
      setAsk('sending', 'Copying the prompt…')
      clipboard.writeText(prompt).then(
        () => setAsk('copied', 'Copied · paste it into the composer'),
        () => setAsk('failed', 'Copy failed — the prompt is shown under the document'),
      )
      return 'sending'
    }
    setAsk('failed', 'Copy failed — this view blocks clipboard access')
    return 'failed'
  }

  /**
   * Hand the takeover to the harness when the board runs inside DSH: the client
   * half installs a bridge that opens a workspace session and injects the prompt
   * as its draft. Outside DSH the Codex follow-up path below still applies.
   */
  function askDsh(handoff: Handoff): AskState {
    const bridge = dshBridge()
    if (!bridge?.startSession) {
      setAsk('failed', 'DSH 会话桥接没有就绪 — 改用 Copy 复制提示词。')
      return 'failed'
    }
    const prompt = askPrompt(handoff)
    lastAskPrompt.value = prompt
    setAsk('sending', 'Starting a DSH session for this handoff…')
    bridge.startSession({ prompt, project: currentProject.value }).then(
      () => setAsk('sent', 'Sent · DSH opened a session with this prompt'),
      (error) => setAsk('failed', dshFailureNotice(error)),
    )
    return 'sending'
  }

  /**
   * The click stays synchronous: the host only accepts a follow-up while the
   * click still holds transient user activation, so nothing is awaited before
   * `sendFollowUpMessage`. Its promise is watched instead, because the host
   * rejects with "A message is already being submitted to this chat" while
   * another turn is starting, and that rejection used to fail silently.
   */
  function askCodex(handoff: Handoff): AskState {
    if (dshBridge()?.startSession) return askDsh(handoff)

    const now = Date.now()
    if (now < askLockUntil) return askState.value
    askLockUntil = now + 2000

    const prompt = askPrompt(handoff)
    lastAskPrompt.value = prompt
    const send = window.openai?.sendFollowUpMessage

    if (typeof send === 'function') {
      let result: unknown
      try {
        result = send({ prompt, title: 'Continue handoff' })
      } catch (error) {
        setAsk('failed', failureNotice(error))
        return 'failed'
      }
      setAsk('sending', 'Asking Codex to take over this handoff…')
      if (result && typeof (result as PromiseLike<unknown>).then === 'function') {
        ;(result as Promise<unknown>).then(
          () => setAsk('sent', 'Sent · Codex starts a new turn in this chat'),
          (error) => setAsk('failed', failureNotice(error)),
        )
        return 'sending'
      }
      setAsk('sent', 'Sent · Codex starts a new turn in this chat')
      return 'sent'
    }

    return copyPrompt(handoff)
  }
  function load(data: Board | null) {
    board.value = data
    if (!data) return
    const withHandoff = data.projects.find((p) => p.handoff_count > 0)
    const first = withHandoff ?? data.projects[0]
    if (first) selectProject(first.path)
  }

  // Selecting a project from the pinned list must not lose the search context.
  watch(projects, () => {
    if (!selectedProject.value && projects.value.length) {
      const withHandoff = projects.value.find((p) => p.handoff_count > 0)
      selectProject((withHandoff ?? projects.value[0]).path)
    }
  })

  return {
    board,
    query,
    onlyHandoffs,
    selectedProject,
    selectedSession,
    selectedHandoff,
    projects,
    stats,
    currentProject,
    currentHandoff,
    visibleProjects,
    pinnedProject,
    orphanHandoffs,
    visibleSessions,
    handoffChoices,
    askState,
    askNotice,
    lastAskPrompt,
    selectProject,
    selectSession,
    selectHandoff,
    askPrompt,
    currentAskPrompt,
    askCodex,
    askDsh,
    copyPrompt,
    load,
  }
}
