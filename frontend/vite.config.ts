import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    port: 5175,
    // Calls go to /api/... on the same origin in development, so no CORS
    // preflight and no absolute URLs baked into the bundle.
    proxy: {
      "/api": {
        // 8011 rather than the usual 8000: this machine already runs another
        // service there. Override with VITE_PROXY_TARGET if yours does not.
        target: process.env.VITE_PROXY_TARGET ?? "http://127.0.0.1:8011",
        changeOrigin: true,
        ws: true,
      },

    },
  },
});
