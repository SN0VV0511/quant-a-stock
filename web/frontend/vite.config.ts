import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

const apiProxy = {
  "/quantify/api": {
    target: "http://127.0.0.1:8888",
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/quantify/, "")
  }
};

export default defineConfig({
  plugins: [react()],
  base: "/quantify/",
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    sourcemap: true,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom"],
          charts: ["chart.js"],
          motion: ["framer-motion"],
          icons: ["lucide-react"]
        }
      }
    }
  },
  server: {
    port: 5173,
    proxy: apiProxy
  },
  preview: {
    proxy: apiProxy
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts"
  }
});
