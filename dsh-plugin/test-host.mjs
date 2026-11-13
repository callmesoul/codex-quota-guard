import { apply } from './lib/index.js'

let handler = null
const ctx = {
  inject(names, run) {
    run({
      effect(fn) { fn() },
      webServer: {
        register(route) { handler = route.handler; return () => {} },
      },
    })
  },
}

apply(ctx)
if (!handler) throw new Error('route was not registered')

function call(url, method = 'GET') {
  return new Promise((resolve, reject) => {
    const res = {
      status: 0,
      headers: null,
      body: '',
      writeHead(status, headers) { this.status = status; this.headers = headers },
      end(payload) { this.body = payload ?? ''; resolve(this) },
    }
    Promise.resolve(handler({ method, url }, res)).catch(reject)
  })
}

const health = await call('/handoff-board/health')
console.log('health', health.status, health.body)

const res = await call('/handoff-board/board')
console.log('board status', res.status, 'bytes', res.body.length)
const board = JSON.parse(res.body)
console.log('stats', JSON.stringify(board.stats))
console.log('codex_home', board.codex_home)
for (const project of board.projects) {
  console.log(
    project.path,
    '| local:', project.local_path,
    '| ws:', project.workspace_id,
    '| handoffs:', project.handoff_count,
    '| sessions:', project.session_count,
  )
}
const first = board.projects.find((p) => p.handoff_count > 0)
if (first) {
  console.log('first handoff:', first.handoffs[0].id, 'md bytes:', (first.handoffs[0].markdown || '').length)
}