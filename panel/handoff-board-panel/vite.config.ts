import { fileURLToPath, URL } from 'node:url'
import tailwindcss from '@tailwindcss/vite'
import vue from '@vitejs/plugin-vue'
import { defineConfig, type Plugin } from 'vite'

/**
 * Codex inline visualizations receive an HTML *fragment*, not a document: the
 * host wraps it in its own <html>/<body> and renders it in a sandboxed iframe
 * whose CSP allows inline <script>/<style> but forbids every network request.
 *
 * So the build is a library build (no index.html involved) and this plugin
 * folds the IIFE bundle plus the CSS into one body-level fragment.
 */
function inlineFragment(): Plugin {
  return {
    name: 'handoff-board-inline-fragment',
    enforce: 'post',
    generateBundle(_options, bundle) {
      const css = Object.values(bundle)
        .filter((item) => item.type === 'asset' && item.fileName.endsWith('.css'))
        .map((item) => String(item.source))
        .join('\n')
      const js = Object.values(bundle)
        .filter((item) => item.type === 'chunk' && item.isEntry)
        .map((item) => item.code)
        .join('\n')

      if (!js) {
        this.error('no entry chunk found to inline')
      }

      const escape = (code: string) => code.replace(/<\/script/gi, '<\\/script')
      const fragment = [
        '<!-- Board payload, replaced by handoff_board.py. -->',
        '<script type="application/json" id="hb-data">null</script>',
        '<div id="hb-root"></div>',
        css ? `<style>${escape(css)}</style>` : '',
        `<script type="module">${escape(js)}</script>`,
      ]
        .filter(Boolean)
        .join('\n')

      this.emitFile({ type: 'asset', fileName: 'panel.fragment.html', source: fragment })
    },
  }
}

export default defineConfig({
  plugins: [vue(), tailwindcss(), inlineFragment()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  // The host frame has no Node shim: some Vue internals still probe
  // process.env.NODE_ENV, so fold it to a literal at build time.
  define: {
    'process.env.NODE_ENV': JSON.stringify('production'),
  },
  build: {
    target: 'es2022',
    cssCodeSplit: false,
    assetsInlineLimit: 1024 * 1024,
    modulePreload: false,
    lib: {
      entry: fileURLToPath(new URL('./src/main.ts', import.meta.url)),
      formats: ['iife'],
      name: 'HandoffBoard',
      fileName: () => 'panel.js',
    },
  },
})
