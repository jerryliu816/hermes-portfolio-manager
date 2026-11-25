import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // The API and the built UI share an origin in production, so the dev proxy
    // mirrors that rather than introducing CORS only in development.
    proxy: {
      "/api": { target: "http://localhost:8787", changeOrigin: true },
      "/health": { target: "http://localhost:8787", changeOrigin: true },
      "/login": { target: "http://localhost:8787", changeOrigin: true },
      "/logout": { target: "http://localhost:8787", changeOrigin: true },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
