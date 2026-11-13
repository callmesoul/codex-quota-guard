import { fileURLToPath, URL } from 'node:url'
import tailwindcss from '@tailwindcss/vite'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

/**
 * Build the DSH client half of the Handoff Board plugin.
 *
 * DSH loads a client plugin as one CommonJS module node whose `require` calls
 * resolve against the host module table, so React stays external and Vue (which
 * the panel app is written in) is bundled in. The output is wrapped by
 * `scripts/wrap-dsh-client.mjs` into the `window.__ModuleLoader__` shell DSH
 * expects, and lands in `dsh-plugin/lib/client.js`.
 */
export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  define: {
    'process.env.NODE_ENV': JSON.stringify('production'),
  },
  build: {
    target: 'es2022',
    outDir: fileURLToPath(new URL('../../dsh-plugin/lib', import.meta.url)),
    emptyOutDir: false,
    cssCodeSplit: false,
    minify: 'oxc',
    lib: {
      entry: fileURLToPath(new URL('./src/dsh/client.ts', import.meta.url)),
      formats: ['cjs'],
      fileName: () => 'client.raw.js',
    },
    rollupOptions: {
      external: ['react', 'react/jsx-runtime', 'react-dom', 'react-dom/client'],
      output: { inlineDynamicImports: true },
    },
  },
})