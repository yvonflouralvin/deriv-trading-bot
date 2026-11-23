import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// En développement, /api est relayé vers l'API locale (port 8000).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { '/api': { target: 'http://localhost:8000', ws: true } },
  },
})
