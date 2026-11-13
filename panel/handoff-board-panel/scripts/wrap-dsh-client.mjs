/**
 * Wrap the Vite CommonJS output in the `window.__ModuleLoader__` shell DSH uses
 * to hand a client bundle its own module record and the host `require`.
 */
import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const MODULE_ID = 'dsh-handoff-board'
const root = dirname(dirname(fileURLToPath(import.meta.url)))
const raw = join(root, '..', '..', 'dsh-plugin', 'lib', 'client.raw.js')
const out = join(root, '..', '..', 'dsh-plugin', 'lib', 'client.js')

const body = readFileSync(raw, 'utf8')
const wrapped = `window.__ModuleLoader__.load({
  id: ${JSON.stringify(MODULE_ID)},
  factory: function(require) {
    var module = { exports: {} };
    var exports = module.exports;
    Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
${body}
    return module.exports;
  }
});
`

writeFileSync(out, wrapped)
console.log(`wrapped ${raw} -> ${out} (${wrapped.length} bytes)`)