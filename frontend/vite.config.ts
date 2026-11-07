/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const api = process.env.AJGEM_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The browser talks to /api; Vite forwards to the FastAPI backend without the prefix.
    proxy: { "/api": { target: api, changeOrigin: true, rewrite: (p) => p.replace(/^\/api/, "") } },
  },
  build: { sourcemap: true, chunkSizeWarningLimit: 1200 },
  test: { environment: "jsdom", globals: true, setupFiles: ["src/test-setup.ts"] },
});
