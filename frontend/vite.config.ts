import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const API_TARGET = "http://127.0.0.1:8765";

export default defineConfig({
  base: "/",
  plugins: [react()],
  build: {
    outDir: "dist",
    assetsDir: "assets",
  },
  server: {
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: true },
      "/mcp": { target: API_TARGET, changeOrigin: true },
    },
  },
});
