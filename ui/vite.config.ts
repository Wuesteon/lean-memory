import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../console/src/lean_memory_console/static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/views": "http://127.0.0.1:8377",
      "/v1": "http://127.0.0.1:8377",
    },
  },
});
