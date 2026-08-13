import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

// Dev server proxies /api and /ws to the FastAPI process (main.py, port 3000)
// so `npm run dev` can run against real data without a CORS dance. The
// production build is served BY FastAPI itself from static/dist/.
//
// base was '/next/' — a leftover from when this dashboard was built
// alongside the old one and lived at that path. The cutover moved it to '/'
// and nobody has navigated to /next since, but the build kept baking
// /next/assets/... into the shell, so the app on port 3000 still depended
// on a mount named after a migration that finished long ago. That made a
// caching bug genuinely hard to reason about: the shell came from one route
// and its assets from another.
export default defineConfig({
  plugins: [svelte()],
  base: '/',
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
