import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

// Dev server proxies /api and /ws to the FastAPI process (main.py, port 3000)
// so `npm run dev` can run against real data without a CORS dance. The
// production build is served BY FastAPI itself — see main.py's mount of
// static/dist/ at /next — so `base` matches that mount path.
export default defineConfig({
  plugins: [svelte()],
  base: '/next/',
  build: {
    outDir: '../static/dist',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://localhost:3000',
      '/ws': { target: 'ws://localhost:3000', ws: true },
    },
  },
})
